"""Pure, bounded project-draft comparison. No LLM, network or write operations."""
import hashlib
import json
from datetime import datetime, timezone
from .project_identity import clues, explicit_scope_conflict, normalize_name
from .project_record import PREFIX, parse_record, record_policy, resolve_replacements


def line_endings(text):
    return text.replace('\r\n', '\n').replace('\r', '\n')


def declaration(content):
    first = content.lstrip().splitlines()[0] if isinstance(content, str) and content.strip() else ''
    if len(first) > 2048 or not first.startswith(PREFIX) or not first.endswith(' -->'):
        return None, None
    try:
        raw = json.loads(first[len(PREFIX):-4])
        if not isinstance(raw, dict) or not isinstance(raw.get('project'), str) or not raw['project'].strip():
            return None, None
        identity = clues(project=raw['project'])
        return (raw, identity) if parse_record(content, identity['name_keys']) else (None, None)
    except (ValueError, TypeError):
        return None, None


def facts(raw):
    values = raw.get('facts', {})
    if not isinstance(values, dict) or len(values) > 8 or any(
        not isinstance(k, str) or not 0 < len(k) <= 64 or
        not isinstance(v, str) or not 0 < len(v) <= 200 for k, v in values.items()
    ):
        return None
    return values


def comparable(a, b):
    # As-of alone does not describe applicability. Equal as-of is comparable;
    # otherwise both sides must explicitly declare bounded, overlapping intervals.
    if datetime.fromisoformat(a['as_of']) == datetime.fromisoformat(b['as_of']):
        return True
    keys = ('valid_from', 'valid_until')
    if not all(k in a and k in b for k in keys):
        return False
    af, au = (datetime.fromisoformat(a[k]) for k in keys)
    bf, bu = (datetime.fromisoformat(b[k]) for k in keys)
    return af < bu and bf < au


def assess(category, title, summary, content, pages, *, now=None, unknowns=()):
    """Compare only supplied full cloud pages; a bounded check is never a full-library claim."""
    result = dict(state='checked', scope=None, coverage='returned_candidates_only',
                  global_dedup_verified=False, candidates=[], unknowns=list(unknowns),
                  needs_confirmation=False)
    if category != 'projects':
        return {**result, 'state': 'not_applicable', 'reason': 'project_handoff_only'}
    now = now or datetime.now(timezone.utc)
    if len(pages) > 3:
        result['unknowns'].append('candidate_limit_exceeded')
    draft_current = False
    draft, identity = declaration(content)
    if draft is None:
        result['unknowns'].append('draft_project_declaration_unknown')
    else:
        result['scope'] = draft['project']
        policy = record_policy(content, identity['name_keys'], now)
        draft_current = (policy['time_state'] not in ('invalid', 'future', 'expired')
                         and policy['record_kind'] not in ('history', 'test'))
        if policy['time_state'] in ('invalid', 'future', 'expired') or policy['record_kind'] in ('history', 'test'):
            result['unknowns'].append('draft_not_current_or_invalid_policy')
        if facts(draft) is None:
            result['unknowns'].append('draft_facts_invalid')
    contents, declarations = {}, {}
    for page in pages[:3]:
        path = page.get('path')
        row = {'path': path, 'classification': 'unverified', 'updated': page.get('updated')}
        result['candidates'].append(row)
        text = page.get('content')
        if isinstance(text, str):
            row['content_sha256'] = hashlib.sha256(text.encode('utf-8')).hexdigest()
        if isinstance(page.get('front_matter'), dict):
            row['metadata_sha256'] = hashlib.sha256(json.dumps(page['front_matter'], ensure_ascii=False,
                sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
        if page.get('assessment_error'):
            result['unknowns'].append('candidate_read_unavailable')
            continue
        if (page.get('source') != 'lingxi_cloud' or page.get('content_scope') != 'full'
                or page.get('truncated') is not False or not isinstance(text, str)
                or not isinstance(page.get('front_matter'), dict)):
            result['unknowns'].append('candidate_not_fully_verified')
            continue
        candidate, candidate_identity = declaration(text)
        if identity and (explicit_scope_conflict(identity, [page, page['front_matter']]) or
                        (candidate and normalize_name(candidate['project']) not in identity['name_keys'])):
            row['classification'] = 'foreign_project'
            continue
        if not draft or not candidate:
            row['classification'] = 'scope_unknown'
            result['unknowns'].append('candidate_scope_unknown')
            continue
        row.update(record_policy(text, identity['name_keys'], now))
        contents[path], declarations[path] = text, candidate
        if row['time_state'] == 'invalid' or facts(candidate) is None:
            result['unknowns'].append('candidate_policy_or_facts_invalid')
            continue
        equal = (all(page['front_matter'].get(k) == v for k, v in (('title', title), ('summary', summary)))
                 and line_endings(text) == line_endings(content))
        row['classification'] = 'exact_duplicate' if equal else 'related_changed'
    # Check the complete finite graph before applying kind/time exclusions.
    sentinel = 'wiki/projects/__lx_assessment_draft__'
    if draft and sentinel not in contents:
        resolution = resolve_replacements({**contents, sentinel: content}, identity['name_keys'])
        result['version_resolution'] = {**resolution}
        if resolution.get('selected') == sentinel:
            result['version_resolution']['selected'] = 'proposed_draft'
        if resolution['state'] == 'conflict':
            result['state'] = 'conflict'
            result['unknowns'].append('replacement_conflict')
        if any(p not in contents for p in draft['supersedes']):
            result['unknowns'].append('replacement_target_not_checked')
        superseded = set(resolution.get('superseded', [])) if resolution.get('selected') == sentinel else set()
        for row in result['candidates']:
            old = declarations.get(row['path'])
            if old is None or row['classification'] == 'unverified':
                continue
            if row['path'] in superseded:
                row['classification'] = 'explicitly_superseded'
            elif row.get('record_kind') in ('history', 'test') or row.get('time_state') in ('future', 'expired'):
                row['classification'] = 'non_current_record'
            elif (draft_current and row['classification'] == 'related_changed' and facts(draft) is not None
                  and facts(old) is not None and comparable(draft, old)):
                changed = sorted(k for k in facts(draft).keys() & facts(old).keys() if facts(draft)[k] != facts(old)[k])
                if changed:
                    row.update(classification='potential_conflict', conflict_fields=changed)
    elif draft:
        result['unknowns'].append('reserved_draft_path_collision')
    if result['unknowns'] and result['state'] != 'conflict':
        result['state'] = 'partial'
    result['unknowns'] = list(dict.fromkeys(result['unknowns']))
    result['needs_confirmation'] = (result['state'] in ('partial', 'conflict') or
        any(r['classification'] == 'potential_conflict' for r in result['candidates']))
    return result
