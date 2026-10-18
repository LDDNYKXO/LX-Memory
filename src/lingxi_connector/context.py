"""Shared-background orchestration: bounded read-only retrieval plus a state machine.

This module is the only place that decides how much is read, how long it may take, which
project a request belongs to and what state to report. It never writes: the only operations it
is allowed to call are the read-only ones (`status`, `search`, `read`).
"""
import asyncio
import copy
import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timezone

from .cloud import ConnectorError
from .project_identity import (clues, explicit_scope_conflict, normalize_name, path_key, search_evidence,
                               select_project, strip_credentials)
from .project_record import parse_record, record_policy, resolve_replacements

SEARCH_ATTEMPTS_MAX = 2
SEARCH_RESULT_LIMIT = 20  # Wider than the read window: descriptive handoff titles can rank beyond 10.
PERSONAL_SEARCH_LIMIT = 20
PERSONAL_PROFILE_EXCERPT = 400
PERSONAL_LESSON_EXCERPT = 200
PERSONAL_TITLE_MAX = 120
PERSONAL_SUMMARY_MAX = 240
CORE_MEMORY_READS = 1
PROJECT_CANDIDATES_MAX = 2
VERSION_CANDIDATES_MAX = 5  # Returned search window only; never fetch arbitrary supersedes links.
REPLACEMENT_READS_MAX = 2  # Explicit same-project bridge verification, not another search window.
CONTEXT_TOTAL_TIMEOUT_S = 45.0
READ_MAX_CHARS_PER_CALL = 20000
CONTEXT_TOTAL_CHARS = 40000
OUTPUT_MAX_CHARS = 8000
CACHE_FRESH_SECONDS = 300
CONTEXT_CACHE_MAX_ENTRIES = 16

STATE_COMPLETE = 'complete'
STATE_PARTIAL = 'partial'
STATE_PROJECT_AMBIGUOUS = 'project_ambiguous'
STATE_CONFLICT = 'conflict'
STATE_SERVICE_UNAVAILABLE = 'service_unavailable'

# Core memory is read by its known path, separately from the bounded searches (decision 3), so a
# vague request still gets the collaboration rules even when no project page matches.
CORE_MEMORY_PATH = 'core/核心记忆'

READ_ONLY_OPERATIONS = ('status', 'search', 'read')


def normalize_request(request):
    """Fold a request to the key used for same-round merging."""
    if not isinstance(request, str):
        return ''
    return ' '.join(request.split()).casefold()


def merge_key(epoch, request_id, request, project='', cwd='', remote=''):
    """Same-round merge key: process-local, and invalidated whenever the session epoch moves.

    Every caller-supplied clue that can change the answer belongs to the key. Two requests that
    only share their text but name different projects, directories or repositories must not share
    one retrieval — merging them would hand the second caller the first project's background.
    """
    payload = '|'.join((
        str(epoch if epoch is not None else ''),
        str(request_id or ''),
        normalize_request(request),
        normalize_name(project),
        path_key(cwd),
        strip_credentials(remote) or '',
    ))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def new_request_id():
    return uuid.uuid4().hex


def _empty_result(request_id):
    return {'state': STATE_COMPLETE, 'request_id': request_id,
            'project': {'clue': {}, 'selected': None, 'candidates': []},
            'goal': '', 'progress': {'executed': [], 'current': [], 'pending_verification': []},
            'next_step': '', 'constraints': [], 'sources': [], 'unknowns': [], 'conflicts': [],
            'field_sources': {},
            'budget': {'search_attempts': 0, 'reads': 0, 'chars': 0, 'output_chars': 0,
                       'truncated': False, 'replacement_reads': 0},
            'retrieved_at': None,
            'consumption': {'background_sources': 'returned_cloud_sources_only',
                            'filesystem_background_fallback': False,
                            'extra_search_fallback': False,
                            'latest_state_verified': False,
                            'enforcement': 'consumer_contract_not_host_sandbox'}}


def _unavailable(result, code, message=''):
    result['state'] = STATE_SERVICE_UNAVAILABLE
    result['error'] = {'code': code}
    if message:
        result['error']['message'] = message
    return result


def _as_list(value):
    """Structured page fields may be a single string or a list; keep only non-empty text."""
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple)) else [value]
    return [str(item).strip() for item in values
            if isinstance(item, (str, int, float)) and str(item).strip()]


def _unique(values):
    seen, ordered = set(), []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _updated_key(row):
    """Numeric recency for candidate-window ordering; never used to pick the current record."""
    value = row.get('updated') if isinstance(row, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _is_descriptive_handoff(row):
    title = row.get('title') if isinstance(row, dict) else ''
    if not isinstance(title, str):
        return False
    return '交接' in title or 'handoff' in title.lower()


def _is_core(path):
    return isinstance(path, str) and path.startswith('core/')


HEADING_RE = re.compile(r'^(#{1,3})\s+(.+?)\s*$')
# The cloud pages record their state as prose under a small, stable heading vocabulary. Only these
# headings are read, and only when present: an unknown heading stays in `sources` as data instead of
# being guessed into a field. Every value derived this way carries its heading in `field_sources`.
HEADING_FIELDS = (
    # Pending evidence must win over a generic word such as "进展" in the same heading.
    ('待验证', 'progress.pending_verification'), ('待验收', 'progress.pending_verification'),
    ('下一步', 'next_step'), ('后续', 'next_step'),
    ('项目身份', 'goal'), ('目标', 'goal'),
    ('已确认进展', 'progress.executed'), ('已完成', 'progress.executed'), ('进展', 'progress.executed'),
    ('当前阶段', 'progress.current'), ('当前状态', 'progress.current'), ('现状', 'progress.current'),
    ('边界', 'constraints'), ('限制', 'constraints'), ('禁止', 'constraints'),
    ('规则', 'constraints'), ('约定', 'constraints'),
)


def sections(content):
    """Split markdown content into `(heading, body)` pairs; text before the first heading is dropped.

    Fenced code blocks are opaque: headings, examples and commands inside ``` or ~~~ are data, never
    a field, so page content can never plant the reported goal or next step. The full page stays in
    `sources`, which is where a caller reads it from.
    """
    if not isinstance(content, str) or not content:
        return []
    found, current, body, fenced = [], None, [], False
    for line in content.splitlines():
        marker = line.lstrip()
        if marker.startswith('```') or marker.startswith('~~~'):
            fenced = not fenced
            continue
        if fenced:
            continue
        matched = HEADING_RE.match(line)
        if matched:
            if current is not None:
                found.append((current, '\n'.join(body).strip()))
            current, body = matched.group(2).strip(), []
        elif current is not None:
            body.append(line)
    if current is not None:
        found.append((current, '\n'.join(body).strip()))
    return found


def heading_fields(content):
    """Map known headings to `(field, text)` pairs in document order; unknown headings are ignored."""
    pairs = []
    for heading, body in sections(content):
        if not body:
            continue
        for keyword, field in HEADING_FIELDS:
            if keyword in heading:
                pairs.append((field, body, heading))
                break
    return pairs


def _project_hits(hits):
    """Account-level core pages are background, never project candidates."""
    return [row for row in hits if not _is_core(row.get('path'))]


def _identity_metadata(path, hits, cache):
    rows = []
    for hit in hits:
        if hit.get('path') == path:
            rows.append(hit)
            if isinstance(hit.get('front_matter'), dict):
                rows.append(hit['front_matter'])
    if path in cache:
        rows.append(cache[path][1])
    return rows


def _matching_pages(clue_set, query, hits):
    exact = select_project(clue_set, hits)
    weak = search_evidence(clue_set, query, hits)
    pages = {}
    for hit in hits:
        for candidate in select_project(clue_set, [hit])['candidates']:
            pages.setdefault(candidate['path'], candidate)
    for candidate in weak['candidates']:
        pages.setdefault(candidate['path'], candidate)
    template = exact if exact['state'] != 'none' else weak
    return {**template, 'state': 'none' if not pages else 'ok' if len(pages) == 1 else STATE_PROJECT_AMBIGUOUS,
            'candidates': list(pages.values())}


async def _read(fetch, path, result, verify_path=False):
    """Read one page inside the remaining character budget.

    Returns `(truncated, front_matter, content)`: the caller decides what the structured fields and
    the prose headings mean, because meaning depends on all sources together.
    """
    budget = result['budget']
    remaining = CONTEXT_TOTAL_CHARS - budget['chars']
    if remaining <= 0:
        return True, {}, ''
    data = await fetch('read', {'path': path, 'offset': 0, 'max_chars': min(READ_MAX_CHARS_PER_CALL, remaining)})
    if not isinstance(data, dict):
        raise ConnectorError('UPSTREAM_INCOMPATIBLE')
    if verify_path and data.get('path') != path:
        raise ConnectorError('UPSTREAM_INCOMPATIBLE')
    content = data.get('content') if isinstance(data.get('content'), str) else ''
    budget['reads'] += 1
    budget['chars'] += len(content)
    truncated = data.get('truncated') is True
    budget['truncated'] = budget['truncated'] or truncated
    result['sources'].append({'path': path, 'updated': data.get('updated'),
                              'retrieved_at': data.get('retrieved_at'),
                              'source': data.get('source'),
                              'content_scope': data.get('content_scope'),
                              'chars': len(content), 'content': content})
    front = data.get('front_matter') if isinstance(data.get('front_matter'), dict) else {}
    return truncated, front, content


def _replacement_frontier(contents, markers, policies, original, attempted):
    """Prioritize gaps in larger known components; never guess a path or rank a winner by date."""
    graph = {path: set(marker['supersedes']) & contents.keys() if marker else set()
             for path, marker in markers.items()}
    replaced = set().union(*graph.values()) if graph else set()
    weights = dict.fromkeys(graph, 0)
    for root in set(graph) - replaced:
        seen, pending = set(), [root]
        while pending:
            path = pending.pop()
            if path not in seen:
                seen.add(path)
                pending.extend(graph[path])
        for path in seen:
            weights[path] = max(weights[path], len(seen & original))
    options = []
    for index, (path, marker) in enumerate(markers.items()):
        policy = policies[path]
        if not marker or policy['record_kind'] not in ('unknown', 'handoff') or policy['time_state'] not in ('undeclared', 'in_range'):
            continue
        for order, target in enumerate(marker['supersedes']):
            if target not in contents and target not in attempted:
                options.append((-weights[path], index, order, path, target))
    return sorted(options)


def _supplement_resolution(contents, markers, policies, name_keys):
    """Validate every known edge before resolving only current-purpose records."""
    full = resolve_replacements(contents, name_keys)
    if full['state'] == STATE_CONFLICT:
        return full
    if any(policies[path]['record_kind'] in ('history', 'test') and marker and
           any(target in policies and policies[target]['record_kind'] in ('unknown', 'handoff')
               for target in marker['supersedes']) for path, marker in markers.items()):
        return {'state': STATE_CONFLICT, 'reason': '测试或历史记录声称替代当前用途记录。'}
    return resolve_replacements({p: text for p, text in contents.items()
                                 if policies[p]['record_kind'] in ('unknown', 'handoff')}, name_keys)


async def _complete_replacement_evidence(fetch, result, contents, markers, policies,
                                         read_cache, decisions, clue_set, hits, evaluated_at,
                                         clock, started):
    """At most two explicit reference reads inside the existing shared time/character budgets.

    Supplemented pages supply graph evidence only. They never become search candidates or
    provide goal/current/next-step fields, even when their prose happens to match the request.
    """
    original = {p for p in contents if policies[p]['record_kind'] in ('unknown', 'handoff')}
    attempted, bridges = set(read_cache), set()
    identities = [row for p in contents for row in _identity_metadata(p, hits, read_cache)]
    audit = {'limit': REPLACEMENT_READS_MAX, 'attempts': [], 'state': 'incomplete'}
    result['project']['replacement_evidence'] = audit
    resolution = _supplement_resolution(contents, markers, policies, clue_set['name_keys'])
    while resolution['state'] == STATE_PROJECT_AMBIGUOUS:
        frontier = _replacement_frontier(contents, markers, policies, original, attempted)
        if not frontier:
            audit['reason'] = 'no_explicit_bridge'
            break
        if len(audit['attempts']) >= REPLACEMENT_READS_MAX:
            audit['reason'] = 'supplement_limit'
            break
        remaining = CONTEXT_TOTAL_TIMEOUT_S - (clock() - started)
        if remaining <= 0 or result['budget']['chars'] >= CONTEXT_TOTAL_CHARS:
            audit['reason'] = 'shared_budget_exhausted'
            break
        _, _, _, parent, path = frontier[0]
        if any(c in path for c in ('%', ':', '\x00')) or any(ord(c) < 32 for c in path):
            audit['reason'] = 'unsafe_reference'
            break
        # Identity conflicts known from search metadata must be rejected before another read.
        if explicit_scope_conflict(clue_set, _identity_metadata(path, hits, read_cache)):
            audit['reason'] = 'referenced_identity_mismatch'
            break
        attempted.add(path)
        attempt = {'from': parent, 'path': path, 'state': 'requested'}
        audit['attempts'].append(attempt)
        result['budget']['replacement_reads'] = len(audit['attempts'])
        try:
            data = await asyncio.wait_for(_read(fetch, path, result, verify_path=True), timeout=remaining)
        except (ConnectorError, asyncio.TimeoutError) as error:
            attempt['state'] = 'read_failed'
            audit['reason'] = 'time_budget' if isinstance(error, asyncio.TimeoutError) else 'read_failed'
            break
        truncated, front, content = data
        source = result['sources'][-1]
        # All supplemental prose is optional graph evidence, never current background.
        source.update(body_chars=len(content), content='', omitted_reason='replacement_evidence_only')
        marker = parse_record(content, clue_set['name_keys'])
        policy = record_policy(content, clue_set['name_keys'], evaluated_at)
        decisions[path] = {'path': path, **policy, 'updated': source.get('updated'),
                           'retrieved_at': source.get('retrieved_at'), 'decision': 'replacement_rejected'}
        result['project']['retrieval_decisions'] = list(decisions.values())
        if (truncated or source.get('content_scope') != 'full' or source.get('source') != 'lingxi_cloud'
                or clock() - started >= CONTEXT_TOTAL_TIMEOUT_S):
            attempt['state'] = 'incomplete'
            audit['reason'] = 'incomplete_source_or_time_budget'
            break
        if (not marker or explicit_scope_conflict(clue_set, [front])
                or policy['record_kind'] not in ('unknown', 'handoff')
                or policy['time_state'] not in ('undeclared', 'in_range')):
            attempt['state'] = 'rejected'
            audit['reason'] = 'unverified_scope_kind_or_time'
            break
        combined = identities + [front]
        remotes = {strip_credentials(row.get('remote')) for row in combined if row.get('remote')}
        remotes.discard(None)
        directories = {path_key(row.get('cwd')) for row in combined if row.get('cwd')}
        if len(remotes) > 1 or (not remotes and len(directories) > 1):
            attempt['state'] = 'rejected'
            audit['reason'] = 'identity_scope_mismatch'
            break
        identities.append(front)
        read_cache[path] = data
        contents[path], markers[path], policies[path] = content, marker, policy
        bridges.add(path)
        attempt['state'] = 'verified_same_project'
        decisions[path].update(decision='replacement_evidence',
                               reason='仅核对明确同项目替代关系，不提供当前事实。')
        resolution = _supplement_resolution(contents, markers, policies, clue_set['name_keys'])
    audit['state'] = 'resolved' if resolution['state'] == 'ok' else resolution['state'] if resolution['state'] == STATE_CONFLICT else 'incomplete'
    return resolution, bridges


def _unverified_notice(rows, sample=3):
    names = '、'.join(row['path'] for row in rows[:sample])
    more = '' if len(rows) <= sample else f' 等 {len(rows)} 份'
    return ('已发现但未核验的同项目候选（超出读取上限）：' + names + more
            + '；当前结论不覆盖这些记录，不能据此认定不存在分叉或冲突。')


def _replacement_gap(result, resolution, contents, markers, current):
    """Known project, unknown version: do not ask the user to choose the same project again."""
    referenced = {p for marker in markers.values() if marker for p in marker['supersedes'] if p in contents}
    roots = [c for c in current if c['path'] not in referenced]
    result['project']['candidates'] = (roots or current)[:PROJECT_CANDIDATES_MAX]
    result['project']['evidence_gap'] = {'kind': 'replacement_chain', 'reason': resolution.get('reason')}
    result['state'] = STATE_PARTIAL
    result['next_step'] = '项目身份已确认，当前版本替代证据不足；只需核对具体替代关系，不需重述项目背景。'
    result['unknowns'].append('同项目版本证据不足，未任选当前记录；补证失败或超限均不代表存在另一个项目。')
    return result


async def _load_personal(fetch, result, clue_set, read_cache):
    """Load a compact personal layer (profile + relevant lessons) when one search slot remains.

    The main project retrieval may use one or two searches; this helper never exceeds the same
    `SEARCH_ATTEMPTS_MAX`.  It degrades silently: personal background is optional and must not
    turn a complete project answer into partial.
    """
    if result['budget']['search_attempts'] >= SEARCH_ATTEMPTS_MAX:
        return
    query = '用户画像 协作偏好 经验 教训'
    result['budget']['search_attempts'] += 1
    try:
        found = await fetch('search', {'query': query, 'limit': PERSONAL_SEARCH_LIMIT})
    except ConnectorError:
        return
    rows = [row for row in (found.get('results') if isinstance(found, dict) else []) if isinstance(row, dict)]
    profiles = sorted([row for row in rows if str(row.get('path', '')).startswith('wiki/profile/')],
                      key=_updated_key, reverse=True)
    lessons = sorted([row for row in rows if str(row.get('path', '')).startswith('wiki/lessons/')],
                     key=_updated_key, reverse=True)
    picks = ([('profile', profiles[0])] if profiles else []) + [('lesson', row) for row in lessons[:2]]
    if not picks:
        return
    loaded = []
    for kind, row in picks:
        path = row.get('path')
        if not isinstance(path, str):
            continue
        try:
            if path in read_cache:
                truncated, front, content = read_cache[path]
            else:
                truncated, front, content = await _read(fetch, path, result)
                read_cache[path] = (truncated, front, content)
        except ConnectorError:
            continue
        source = next((item for item in reversed(result['sources']) if item.get('path') == path), {})
        if truncated or source.get('content_scope') != 'full' or source.get('source') != 'lingxi_cloud':
            continue
        loaded.append((kind, path, front, content))
    if not loaded:
        return
    profile = next((item for item in loaded if item[0] == 'profile'), None)
    lesson_items = [item for item in loaded if item[0] == 'lesson']
    def compact(item, limit):
        _, path, front, content = item
        title = front.get('title') if isinstance(front.get('title'), str) else ''
        summary = front.get('summary') if isinstance(front.get('summary'), str) else ''
        # Title and summary are attacker-adjacent page metadata, not excerpts: bound them too, or a
        # long summary would bypass the excerpt budget and dominate the output.
        return {'path': path, 'title': title[:PERSONAL_TITLE_MAX],
                'summary': summary[:PERSONAL_SUMMARY_MAX],
                'excerpt': content[:limit]}
    personal = {}
    if profile:
        personal['profile'] = compact(profile, PERSONAL_PROFILE_EXCERPT)
    if lesson_items:
        personal['lessons'] = [compact(item, PERSONAL_LESSON_EXCERPT) for item in lesson_items]
    if not personal:
        return
    result['personal'] = personal
    paths = [item[1] for item in loaded]
    result['field_sources']['personal'] = {'path': paths[0],
                                           'origins': [{'path': path} for path in paths]}


async def _build_context(fetch, request='', project='', cwd='', remote='', request_id=None, now=None,
                         include_personal=False):
    """Read the bounded background for one request. Read-only: status/search/read are the only calls.

    `fetch(operation, args)` is awaited by this function; the caller owns the session and the
    timeout of the underlying transport.
    """
    result = _empty_result(request_id if isinstance(request_id, str) and request_id else new_request_id())
    try:
        status = await fetch('status', {})
    except ConnectorError as error:
        return _unavailable(result, error.code, str(error))
    if not isinstance(status, dict) or status.get('cloud_available') is not True:
        return _unavailable(result, (status or {}).get('error') or 'AUTH_REQUIRED')
    result['project']['clue'] = clues(request=request, project=project, cwd=cwd, remote=remote)
    clue_set = result['project']['clue']
    clock = now if callable(now) else time.monotonic
    started = clock()
    queries = [query for query in clue_set.get('queries', []) if query][:SEARCH_ATTEMPTS_MAX]
    hits, selection = [], {'state': 'none', 'candidates': []}
    evaluated_at = datetime.now(timezone.utc)
    result['project']['policy_evaluated_at'] = evaluated_at.isoformat()
    decisions = {}
    result['project']['retrieval_decisions'] = []
    read_cache = {}
    foreign = set()
    try:
        for query in queries:
            if clock() - started > CONTEXT_TOTAL_TIMEOUT_S:
                result['state'] = STATE_PARTIAL
                result['unknowns'].append('时间预算不足，未完成项目检索。')
                return result
            result['budget']['search_attempts'] += 1
            found = await fetch('search', {'query': query, 'limit': SEARCH_RESULT_LIMIT})
            rows = found.get('results') if isinstance(found, dict) else None
            hits.extend(row for row in (rows or []) if isinstance(row, dict))
            for row in _project_hits(hits):
                path = row.get('path')
                if explicit_scope_conflict(clue_set, _identity_metadata(path, hits, read_cache)):
                    foreign.add(path)
                    decisions[path] = {'path': path, 'decision': 'foreign_identity',
                                       'reason': '明确项目或仓库身份不匹配，未采用。'}
            result['project']['retrieval_decisions'] = list(decisions.values())
            eligible = [row for row in _project_hits(hits) if row.get('path') not in foreign]
            selection = _matching_pages(clue_set, query, eligible)
            if selection['state'] != 'none':
                break
    except ConnectorError as error:
        return _unavailable(result, error.code, str(error))
    selection_candidates = selection['candidates']
    result['project']['candidate_count'] = len(selection_candidates)
    version_candidates = selection_candidates
    # Candidates dropped only to fit the read budget are still *known* same-project records.
    # Writing `complete` while ignoring them would hide a possible fork/conflict, so they are
    # reported as an explicit evidence gap instead of being silently discarded.
    unverified_candidates = []
    if len(version_candidates) > VERSION_CANDIDATES_MAX:
        # A descriptive handoff title ("Project 交接：…") can rank outside the short-title window.
        # When such a page is the newest result, prefer it plus the most recent remaining candidates:
        # the read budget stays at VERSION_CANDIDATES_MAX and the final version choice still comes
        # from the explicit replacement chain, not from `updated` alone.
        newest = max(version_candidates, key=_updated_key)
        explicit = [row for row in version_candidates
                    if _is_descriptive_handoff(row) and _updated_key(row) >= _updated_key(newest)]
        if explicit:
            rest = [row for row in version_candidates if row not in explicit]
            explicit = sorted(explicit, key=_updated_key, reverse=True)
            rest = sorted(rest, key=_updated_key, reverse=True)
            ordered = explicit + rest
            version_candidates = ordered[:VERSION_CANDIDATES_MAX]
            unverified_candidates = ordered[VERSION_CANDIDATES_MAX:]
        else:
            result['project']['candidates'] = selection_candidates[:PROJECT_CANDIDATES_MAX]
            result['state'] = STATE_PARTIAL
            result['unknowns'].append('版本候选超过有限处理上限，未读取或任选当前记录。')
            return result
    if unverified_candidates:
        result['project']['unverified_candidates'] = [row['path'] for row in unverified_candidates]
        result['unknowns'].append(_unverified_notice(unverified_candidates))
    candidates = version_candidates[:PROJECT_CANDIDATES_MAX]
    result['project']['candidates'] = candidates
    if version_candidates:
        incomplete = False
        policies = {}
        for candidate in version_candidates:
            path = candidate['path']
            if clock() - started > CONTEXT_TOTAL_TIMEOUT_S or result['budget']['chars'] >= CONTEXT_TOTAL_CHARS:
                incomplete = True
                result['unknowns'].append('时间或字符预算不足，候选未核验完整。')
                break
            try:
                read_cache[path] = await _read(fetch, path, result)
            except ConnectorError:
                incomplete = True
                decisions[path] = {'path': path, 'decision': 'read_failed',
                                   'reason': '候选读取失败，未重试；证据不完整。'}
                result['unknowns'].append('候选读取失败，未重试；不能选取当前记录。')
                continue
            truncated, front, content = read_cache[path]
            source = result['sources'][-1]
            policy = record_policy(content, clue_set['name_keys'], evaluated_at)
            policies[path] = policy
            decisions[path] = {'path': path, **policy, 'updated': source.get('updated'),
                               'retrieved_at': source.get('retrieved_at'),
                               'decision': 'considered'}
            if explicit_scope_conflict(clue_set, _identity_metadata(path, hits, read_cache)):
                foreign.add(path)
                decisions[path].update(decision='foreign_identity', reason='读回项目或仓库身份不匹配。')
            if truncated or source.get('content_scope') != 'full' or source.get('source') != 'lingxi_cloud':
                incomplete = True
                result['unknowns'].append('候选正文未完整读取，不能据此确定当前状态。')
                break
        result['project']['retrieval_decisions'] = list(decisions.values())
        if incomplete or len(read_cache) != len(version_candidates) or clock() - started > CONTEXT_TOTAL_TIMEOUT_S:
            result['state'] = STATE_PARTIAL
            result['unknowns'].append('版本证据不完整，未选取项目当前记录。')
            return result
        own = [c for c in version_candidates if c['path'] not in foreign]
        contents = {c['path']: read_cache[c['path']][2] for c in own}
        markers = {path: parse_record(content, clue_set['name_keys']) for path, content in contents.items()}
        if any(marker and set(marker['supersedes']) & foreign for marker in markers.values()):
            result['state'] = STATE_PROJECT_AMBIGUOUS
            result['unknowns'].append('替代声明涉及明确异项目候选，未跨项目裁定。')
            return result
        identities = [row for c in own for row in _identity_metadata(c['path'], hits, read_cache)]
        remotes = {strip_credentials(row.get('remote')) for row in identities if row.get('remote')}
        remotes.discard(None)
        directories = {path_key(row.get('cwd')) for row in identities if row.get('cwd')}
        if len(remotes) > 1 or (not remotes and len(directories) > 1):
            result['state'] = STATE_PROJECT_AMBIGUOUS
            result['unknowns'].append('候选仓库或目录身份证据冲突，不能认定为同一项目。')
            return result
        # Validate the full same-project graph before excluding history/test records.
        full_resolution = resolve_replacements(contents, clue_set['name_keys']) if contents else {}
        cross_kind = any(policies[path]['record_kind'] in ('history', 'test') and marker and
                         any(target in policies and policies[target]['record_kind'] in ('unknown', 'handoff')
                             for target in marker['supersedes']) for path, marker in markers.items())
        if full_resolution.get('state') == STATE_CONFLICT or cross_kind:
            result['state'] = STATE_CONFLICT
            result['conflicts'].append(full_resolution.get('reason') if full_resolution.get('state') == STATE_CONFLICT
                                       else '测试或历史记录声称替代当前用途记录，不能提升为当前状态。')
            result['unknowns'].append('替代冲突未裁定，未选取当前记录。')
            return result
        for path in contents:
            if policies[path]['time_state'] == 'invalid':
                result['state'] = STATE_PROJECT_AMBIGUOUS if markers[path] is None else STATE_PARTIAL
                result['unknowns'].append('项目声明或用途/时间元数据无效，未选取当前记录。')
                return result
        current = [c for c in own if policies[c['path']]['record_kind'] in ('unknown', 'handoff')]
        bridges = set()
        current_resolution = resolve_replacements({c['path']: contents[c['path']] for c in current}, clue_set['name_keys'])
        if (len(current) > 1 and current_resolution['state'] == STATE_PROJECT_AMBIGUOUS
                and all(markers[c['path']] for c in current)):
            full_resolution, bridges = await _complete_replacement_evidence(
                fetch, result, contents, markers, policies, read_cache, decisions,
                {**clue_set, 'remote_key': clue_set.get('remote_key') or next(iter(remotes), '')},
                hits, evaluated_at, clock, started)
            if full_resolution['state'] == STATE_CONFLICT:
                result['state'] = STATE_CONFLICT
                result['conflicts'].append(full_resolution['reason'])
                return result
            if full_resolution['state'] != 'ok':
                return _replacement_gap(result, full_resolution, contents, markers, current)
        for c in own:
            if c not in current:
                decisions[c['path']].update(decision='non_current_kind', reason='明确历史或测试用途，不提供当前状态。')
        if not current:
            result['state'] = STATE_PARTIAL
            result['unknowns'].append('没有可用于当前背景的项目记录；未回退到测试、历史或异项目页。')
            return result
        if len(current) == 1:
            resolution = {'state': 'ok', 'selected': current[0]['path'],
                          'superseded': full_resolution.get('superseded', [])}
        else:
            current_paths = {c['path'] for c in current} | bridges
            resolution = resolve_replacements({p: text for p, text in contents.items() if p in current_paths}, clue_set['name_keys'])
        if resolution['state'] == STATE_CONFLICT:
            result['state'] = STATE_CONFLICT
            result['conflicts'].append(resolution['reason'])
            return result
        if resolution['state'] == 'ok':
            path = resolution['selected']
            if path not in {c['path'] for c in current}:
                return _replacement_gap(result, resolution, contents, markers, current)
            if policies[path]['time_state'] in ('expired', 'future'):
                decisions[path].update(decision='not_applicable', reason='最终记录不在明确适用区间内；不复活被替代旧页。')
                result['state'] = STATE_PARTIAL
                result['unknowns'].append('当前最终记录已过期或尚未适用，项目现状未知。')
                return result
            candidates = [c for c in current if c['path'] == path]
            selection = {**selection, 'state': 'ok', 'candidates': candidates}
            # A dropped candidate named in the `supersedes` list of ANY verified same-project record
            # is definitively historical: that record explicitly replaces it. Union the links from
            # the whole read window, not just the selected page, so a chain that spans several
            # verified pages still accounts for its older members.
            covered = set()
            for marker in markers.values():
                if marker:
                    covered.update(marker['supersedes'])
            if covered and unverified_candidates:
                remaining = [row for row in unverified_candidates if row['path'] not in covered]
                if len(remaining) != len(unverified_candidates):
                    unverified_candidates = remaining
                    result['project']['unverified_candidates'] = [row['path'] for row in remaining]
                    result['unknowns'] = [item for item in result['unknowns']
                                          if '未核验的同项目候选' not in item]
                    if remaining:
                        result['unknowns'].append(_unverified_notice(remaining))
            for c in current:
                decision = decisions[c['path']]
                decision.update(decision='selected' if c['path'] == path else 'superseded',
                                reason='采用有限同项目候选及明确替代证据；未证明实时最新。' if c['path'] == path else '已被明确替代。')
            if len(own) > 1:
                result['project']['version_resolution'] = {
                    'basis': 'explicit_same_project_replacement_chain' if resolution['superseded'] else 'explicit_record_kind',
                    'considered': list(contents), 'superseded': resolution['superseded']}
        else:
            selection = {**selection, 'state': STATE_PROJECT_AMBIGUOUS, 'candidates': current}
            candidates = current[:PROJECT_CANDIDATES_MAX]
            result['unknowns'].append(resolution['reason'])
    result['project']['candidates'] = candidates
    if selection.get('evidence'):
        # Identity equality found nothing; this candidate is the top project-namespace hit of a
        # discriminating query, so label the weaker basis instead of presenting it as confirmed.
        result['project']['evidence'] = selection['evidence']
        for path in selection.get('runner_ups', []):
            result['unknowns'].append('同一查询还命中其它项目页，未采用：' + path)
    if selection['state'] == 'project_ambiguous':
        result['state'] = STATE_PROJECT_AMBIGUOUS
        result['next_step'] = '请先确认要接续的是哪一个项目，再重新提问。'
        result['unknowns'].append('同名项目候选不止一个，未在歧义下任选：'
                                  + '、'.join(candidate['path'] for candidate in candidates))
        return result
    if selection['state'] == 'none':
        result['unknowns'].append('未检索到与该请求匹配的项目背景页面。')
    else:
        result['project']['selected'] = candidates[0]['path']
        result['project']['title'] = candidates[0].get('title')
    plan = [candidates[0]['path']] if selection['state'] == 'ok' else []
    if CORE_MEMORY_PATH not in plan:
        plan.append(CORE_MEMORY_PATH)
    partial = False
    fronts = []
    for path in plan:
        if clock() - started > CONTEXT_TOTAL_TIMEOUT_S:
            partial = True
            result['unknowns'].append('时间预算用尽（>%gs），未读取剩余内容。' % CONTEXT_TOTAL_TIMEOUT_S)
            break
        try:
            truncated, front, content = read_cache[path] if path in read_cache else await _read(fetch, path, result)
        except ConnectorError as error:
            if path == CORE_MEMORY_PATH and error.code == 'PAGE_NOT_FOUND':
                result['unknowns'].append('未找到核心记忆页（%s），本轮没有协作规则可引用。' % CORE_MEMORY_PATH)
                continue
            partial = True
            result['unknowns'].append('读取失败（%s），已按已读到的内容降级回答。' % error.code)
            continue
        marker = parse_record(content, clue_set['name_keys']) if path == result['project']['selected'] else None
        if marker:
            result['record'] = marker
            front = {**front, 'supersedes': _unique(marker['supersedes'] +
                     result['project'].get('version_resolution', {}).get('superseded', []))}
        fronts.append((path, front, content))
        # A page can be incomplete without `truncated` (e.g. content_scope=partial or a
        # non-cloud source); treat it the same as a truncated read for completeness purposes.
        source = next((row for row in reversed(result['sources']) if row.get('path') == path), {})
        if truncated or source.get('content_scope') != 'full' or source.get('source') != 'lingxi_cloud':
            partial = True
    superseded = set()
    for path, front, _ in fronts:
        if _is_core(path):
            continue
        superseded.update(_as_list(front.get('supersedes')))
    live = [(path, front, content) for path, front, content in fronts if path not in superseded]
    if superseded:
        result['unknowns'].append('以下页面已被明确取代，未采用：' + '、'.join(sorted(superseded)))
    project_live = [row for row in live if row[0] == result['project']['selected']]
    def declared_constraints(front, content):
        if 'constraints' in front:
            value = front['constraints']
            if isinstance(value, str):
                return bool(value.strip())
            return isinstance(value, list) and all(isinstance(x, str) and x.strip() for x in value)
        return any(field == 'constraints' for field, _, _ in heading_fields(content))
    declarations = {path: declared_constraints(front, content) for path, front, content in live}
    required_scopes = [CORE_MEMORY_PATH, result['project']['selected']]
    result['_constraints_status'] = ('complete' if not partial and all(declarations.get(path, False)
                                      for path in required_scopes) else 'unknown')
    constraint_origins = [{'path': path, 'heading': heading} for path, front, content in live
                          for field, _, heading in heading_fields(content) if field == 'constraints']
    constraint_origins += [{'path': path, 'field': 'constraints'} for path, front, _ in live if 'constraints' in front]
    result['goal'] = next((str(front.get('goal')).strip() for _, front, _ in project_live if front.get('goal')), '')
    result['progress']['executed'] = _unique([item for _, front, _ in project_live for item in _as_list(front.get('progress'))])
    result['progress']['pending_verification'] = _unique(
        [item for _, front, _ in project_live for item in _as_list(front.get('pending_verification'))])
    result['constraints'] = _unique([item for _, front, _ in live for item in _as_list(front.get('constraints'))])
    steps = [(path, str(front.get('next_step')).strip()) for path, front, _ in project_live
             if isinstance(front.get('next_step'), str) and front.get('next_step').strip()]
    # Prose headings fill only what the structured fields left empty, and always keep their origin.
    for path, front, content in live:
        structured_step = front.get('next_step') if isinstance(front.get('next_step'), str) else ''
        for field, text, heading in heading_fields(content):
            if path != result['project']['selected'] and field != 'constraints':
                continue
            if field == 'goal':
                if not result['goal']:
                    result['goal'] = text
                    result['field_sources']['goal'] = {'path': path, 'heading': heading}
            elif field == 'next_step':
                if structured_step.strip():
                    # Same page, explicit field: the prose mirror does not compete with it.
                    continue
                steps.append((path, text))
                result['field_sources'].setdefault('next_step', {'path': path, 'heading': heading})
            elif field.startswith('progress.'):
                name = field.split('.', 1)[1]
                result['progress'][name] = _unique(result['progress'][name] + [text])
                result['field_sources'].setdefault(field, {'path': path, 'heading': heading})
            else:
                result['constraints'] = _unique(result['constraints'] + [text])
                result['field_sources'].setdefault(field, {'path': path, 'heading': heading})
    if constraint_origins:
        result['field_sources'].setdefault('constraints', {**constraint_origins[0]})['origins'] = constraint_origins
    for path, front, _ in project_live:
        for field, key in (('goal', 'goal'), ('next_step', 'next_step'),
                           ('progress.executed', 'progress'), ('progress.pending_verification', 'pending_verification')):
            if front.get(key):
                result['field_sources'].setdefault(field, {'path': path, 'field': key})
    distinct = _unique([step for _, step in steps])
    if result['budget']['truncated']:
        partial = True
    if partial:
        result['state'] = STATE_PARTIAL
        result['unknowns'].append('未读完所有内容（分页或字符预算用尽），本结论不完整。')
    if len(distinct) > 1:
        # Two sources disagree about what happens next and neither supersedes the other.
        result['state'] = STATE_CONFLICT
        result['next_step'] = ''
        result['conflicts'].append('不同来源给出不同的下一步：'
                                   + '；'.join('%s → %s' % (path, step) for path, step in steps))
        result['unknowns'].append('冲突未裁定前，不要依赖下一步行动。')
    else:
        result['next_step'] = distinct[0] if distinct else ''
    missing = []
    if not result['project']['selected']:
        missing.append('project')
    if not result['goal'].strip():
        missing.append('goal')
    # Pending evidence is not proof of current or completed progress.
    if not any(result['progress'][name] for name in ('executed', 'current')):
        missing.append('progress')
    if not result['next_step'].strip() and result['state'] != STATE_CONFLICT:
        missing.append('next_step')
    if missing:
        result['unknowns'].append('背景必要字段缺失：' + ', '.join(missing))
        if result['state'] == STATE_COMPLETE:
            result['state'] = STATE_PARTIAL
    if unverified_candidates and result['state'] == STATE_COMPLETE:
        # A complete claim must cover every same-project record this request already discovered.
        result['state'] = STATE_PARTIAL
    if include_personal:
        try:
            await _load_personal(fetch, result, clue_set, read_cache)
        except ConnectorError:
            pass
    retrieved = [source['retrieved_at'] for source in result['sources'] if source['retrieved_at']]
    result['retrieved_at'] = max(retrieved) if retrieved else None
    return result


async def build_context(fetch, request='', project='', cwd='', remote='', request_id=None, now=None, raw=False,
                        include_personal=False):
    result = await _build_context(fetch, request, project, cwd, remote, request_id, now,
                                  include_personal=include_personal)
    return result if raw else _fit_output(result)


def _output_chars(result):
    from .context_output import output_chars
    return output_chars(result)


def _fit_output(result, limit=OUTPUT_MAX_CHARS, audit=None):
    from .context_output import background
    fitted = background(result, limit, audit)
    if result.get('personal') and fitted.get('reason') == 'essential_budget_exceeded':
        # The personal layer is optional; essential project background wins over it.
        trimmed = {k: v for k, v in result.items() if k != 'personal'}
        sources = trimmed.get('field_sources')
        if isinstance(sources, dict):
            sources = dict(sources)
            sources.pop('personal', None)
            trimmed['field_sources'] = sources
        fitted = background(trimmed, limit, audit)
    result.clear()
    result.update(fitted)
    return result
