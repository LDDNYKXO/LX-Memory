"""Pre-commit simulation for project handoffs.

The write-side preview (`prepare`) proves what will be written, not that the
new page will become the current context record.  This module feeds a virtual
draft page into the real `build_context` pipeline: same search window, same
candidate ordering, same replacement-chain resolver.  A caller may therefore
run this gate before `commit` and refuse to write unless it returns `ok`.

It is intentionally read-only: the virtual page exists only inside the
injected fetch wrapper and is never sent to the cloud.
"""
import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone

from .cloud import ConnectorError
from .context import build_context

PREFIX = '<!-- memory-project-record '
VALIDATION_TIMEOUT = 45.0


def _record(content):
    """Return the declared record object, or None when no valid declaration exists."""
    if not isinstance(content, str):
        return None
    first = content.splitlines()[0] if content.splitlines() else ''
    if first.startswith(PREFIX):
        raw = first[len(PREFIX):]
    else:
        # Tolerate a leading BOM/whitespace while still requiring the declaration
        # to be the first meaningful line.
        stripped = content.lstrip()
        if not stripped.startswith(PREFIX):
            return None
        raw = stripped.splitlines()[0][len(PREFIX):]
    if '-->' in raw:
        raw = raw.split('-->', 1)[0]
    try:
        value = json.loads(raw.strip())
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _draft_path(identifier):
    digest = hashlib.sha256(str(identifier).encode('utf-8')).hexdigest()[:32]
    return 'wiki/projects/lingxi-connector-validation-' + digest


def _virtual_hit(path, title, summary, project, updated):
    return {
        'path': path,
        'title': title,
        'summary': summary,
        'updated': updated,
        'score': 999.0,
        'front_matter': {'title': title, 'summary': summary, 'project': project},
    }


def _virtual_page(path, title, summary, project, content, updated):
    return {
        'path': path,
        'front_matter': {'title': title, 'summary': summary, 'project': project},
        'content': content,
        'updated': updated,
        'retrieved_at': datetime.now(timezone.utc).isoformat(),
        'source': 'lingxi_cloud',
        'content_total_chars': len(content),
        'offset': 0,
        'truncated': False,
        'next_offset': None,
        'content_scope': 'full',
    }


def _blocked(reason, **extra):
    value = {'state': 'blocked', 'reason': reason}
    value.update(extra)
    return value


def _first_record(record, content, path, project_name):
    """Gate for the first record of a project: no chain exists, so judge the draft's own sections.

    Without this branch a brand-new project could never save its first handoff, because the
    retrieval pipeline reports `partial` precisely when no project page exists yet.
    """
    from .context import heading_fields
    fields = {field for field, _, _ in heading_fields(content)}
    gaps = []
    if not (str(record.get('goal') or '').strip() or 'goal' in fields):
        gaps.append('goal')
    if 'progress.current' not in fields:
        gaps.append('progress.current')
    if not (str(record.get('next_step') or '').strip() or 'next_step' in fields):
        gaps.append('next_step')
    if not (('constraints' in fields) or ('constraints' in record)):
        gaps.append('constraints')
    base = {'predicted_state': 'first_record', 'selected': None, 'draft_path': path,
            'project': project_name, 'as_of': record.get('as_of'), 'candidate_count': 0,
            'unverified_candidates': [], 'essential_gaps': gaps, 'conflicts': [], 'unknowns': [],
            'limitation': 'No same-project record existed yet: the draft is treated as the first '
                          'record, so only its own sections were checked.'}
    if gaps:
        return _blocked('ESSENTIAL_BACKGROUND_INCOMPLETE', **base)
    return {'state': 'ok', **base}


async def validate_proposal(session, category, title, summary, content, path=None,
                            project=None, cwd='', remote='', now=None):
    """Simulate the draft as the newest project page and return a gate result.

    `session` must expose the read-only session methods `status`, `search` and
    `read`; it is never asked to write.  Non-project categories return
    `not_applicable` because this gate only protects replacement chains.
    """
    if category != 'projects':
        return {'state': 'not_applicable', 'reason': 'project_handoff_only'}

    record = _record(content)
    if record is None:
        return _blocked('MISSING_PROJECT_RECORD')
    declared_project = record.get('project')
    if not isinstance(declared_project, str) or not declared_project.strip():
        return _blocked('MISSING_PROJECT')
    record_kind = record.get('record_kind')
    if record_kind not in (None, 'handoff', 'unknown'):
        return _blocked('UNSUPPORTED_RECORD_KIND')
    as_of = _timestamp(record.get('as_of'))
    if as_of is None:
        return _blocked('MISSING_AS_OF')
    current = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if as_of > current:
        return _blocked('AS_OF_IN_FUTURE', as_of=record.get('as_of'),
                        now=current.isoformat())

    project_name = project if isinstance(project, str) and project.strip() else declared_project
    path = path if isinstance(path, str) and path.strip() else _draft_path(title)
    if not path.startswith('wiki/projects/'):
        return _blocked('INVALID_DRAFT_PATH')
    updated = int(current.timestamp())
    hit = _virtual_hit(path, title, summary, project_name, updated)
    virtual_page = _virtual_page(path, title, summary, project_name, content, updated)

    async def fetch(operation, args):
        if operation == 'status':
            # A standalone validator starts with `remember=true` but `state=UNKNOWN`;
            # refresh once so this gate does not mistake an unverified session for offline.
            return await session.status(refresh=True)
        if operation == 'search':
            data = await session.search(args.get('query'), args.get('limit', 20))
            if not isinstance(data, dict):
                raise ConnectorError('UPSTREAM_INCOMPATIBLE')
            rows = [row for row in (data.get('results') or []) if isinstance(row, dict)]
            # The draft is prepended, not appended: a real new page may rank behind
            # old short titles, and the candidate-window logic must still see it.
            rows = [row for row in rows if row.get('path') != path]
            rows.insert(0, dict(hit))
            result = dict(data)
            result['results'] = rows
            total = data.get('total')
            result['total'] = max(len(rows), total if isinstance(total, int) else len(rows))
            return result
        if operation == 'read':
            if args.get('path') == path:
                return dict(virtual_page)
            return await session.read(args.get('path'), args.get('offset', 0),
                                      args.get('max_chars', 8000))
        raise ConnectorError('INVALID_INPUT')

    try:
        result = await asyncio.wait_for(
            build_context(fetch, request=title, project=project_name, cwd=cwd,
                          remote=remote, request_id='precommit-validation',
                          now=time.monotonic, raw=True),
            timeout=VALIDATION_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return _blocked('VALIDATION_TIMEOUT')
    except ConnectorError as error:
        return _blocked('VALIDATION_UNAVAILABLE', error=error.code)
    if not isinstance(result, dict):
        return _blocked('VALIDATION_INCOMPATIBLE')

    predicted = result.get('state')
    selected = (result.get('project') or {}).get('selected')
    candidate_count = (result.get('project') or {}).get('candidate_count') or 0
    # The virtual draft is always one of the candidates, so "no existing record" means the draft is
    # the only one: there is no replacement chain to contradict.
    if candidate_count <= 1 and (selected in (None, path)):
        return _first_record(record, content, path, project_name)
    conflicts = list(result.get('conflicts') or [])
    unknowns = list(result.get('unknowns') or [])
    unverified = (result.get('project') or {}).get('unverified_candidates') or []
    # The raw pipeline treats a project answer with only `progress.executed` as complete, while the
    # published output layer downgrades it. The gate must be at least as strict as what consumers
    # will see, otherwise a commit could pass validation and still produce a partial background.
    essential_gaps = []
    if not selected:
        essential_gaps.append('project')
    if not str(result.get('goal') or '').strip():
        essential_gaps.append('goal')
    progress = result.get('progress') or {}
    # The published output layer requires `progress.current`; history-only progress stays partial,
    # so the gate must not accept it either.
    if not progress.get('current'):
        essential_gaps.append('progress.current')
    if not str(result.get('next_step') or '').strip() and predicted != 'conflict':
        essential_gaps.append('next_step')
    # An empty constraints list is only acceptable when the raw pipeline reports that every
    # required scope declared its constraints; otherwise the boundary is simply unknown.
    constraints_status = result.get('_constraints_status') or (
        'complete' if result.get('constraints') else 'unknown')
    if constraints_status != 'complete':
        essential_gaps.append('constraints')
    if unverified:
        essential_gaps.append('unverified_candidates')
    base = {
        'predicted_state': predicted,
        'selected': selected,
        'draft_path': path,
        'project': project_name,
        'as_of': record.get('as_of'),
        'candidate_count': (result.get('project') or {}).get('candidate_count'),
        'unverified_candidates': unverified,
        'essential_gaps': essential_gaps,
        'conflicts': conflicts,
        'unknowns': unknowns,
        'budget': result.get('budget'),
        'limitation': 'The draft is a virtual search hit; real index ranking/latency is only '
                      'confirmed after commit by search/context.',
    }
    if predicted == 'complete' and selected == path and not conflicts and not essential_gaps:        return {'state': 'ok', **base}
    if essential_gaps and predicted == 'complete' and selected == path:
        return _blocked('ESSENTIAL_BACKGROUND_INCOMPLETE', **base)
    if predicted == 'complete':
        return _blocked('DRAFT_NOT_SELECTED', **base)
    if predicted == 'conflict':
        return _blocked('RETRIEVAL_CONFLICT', **base)
    if predicted == 'project_ambiguous':
        return _blocked('RETRIEVAL_AMBIGUOUS', **base)
    return _blocked('RETRIEVAL_' + str(predicted or 'UNKNOWN').upper(), **base)
