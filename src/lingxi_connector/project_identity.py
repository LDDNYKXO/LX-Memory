"""Project clues: normalize a project name, strip credentials from a git remote, pick candidates.

Read-only and side-effect free: this module never scans the user's directories, review folders
or chat history. Every clue comes from the caller (request text, project name, working directory,
git remote) and is only used as a hint for a bounded cloud search.
"""
import re
import json
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

NAME_SEPARATORS = ' \t-_./\\'
SCP_REMOTE = re.compile(r'^(?:[^@/\s]+@)?([^:/\s]+):(?!//)(.+)$')
QUERY_MAX_CHARS = 2000
# Generic product words carry no identity: a directory named 「通用灵犀记忆产品」must still be able to
# match a cloud page titled 「灵犀记忆 MCP（lingxi-memory-connector）…」. Only whole leading/trailing
# words are dropped, and never below two characters, so this stays a deterministic reduction.
GENERIC_NAME_TOKENS = ('通用', '产品', '项目', '系统', '平台', '助手', '工具', '服务', '应用', '客户端', '共享')


def reduce_name(value):
    """Drop generic product words from both ends so a shorter query can still match the cloud page."""
    text = clean_text(value)
    changed = True
    while changed:
        changed = False
        for token in GENERIC_NAME_TOKENS:
            if len(text) - len(token) >= 2:
                if text.startswith(token):
                    text = text[len(token):].strip()
                    changed = True
                elif text.endswith(token):
                    text = text[:-len(token)].strip()
                    changed = True
    return text


def normalize_name(value):
    """Fold a project name to a comparison key: NFKC, case-insensitive, separators removed."""
    if not isinstance(value, str):
        return ''
    return ''.join(character for character in unicodedata.normalize('NFKC', value).casefold()
                   if character not in NAME_SEPARATORS)


def strip_credentials(remote):
    """Return a credential-free host/owner/repo identity, or None when it cannot be parsed.

    https userinfo and every query/fragment are dropped; scp-style and ssh:// remotes are
    normalized to the same shape. Only this identity is ever reported, never the full URL.
    """
    if not isinstance(remote, str) or not remote.strip():
        return None
    value = remote.strip()
    matched = SCP_REMOTE.match(value)
    if matched:
        host, path = matched.group(1), matched.group(2)
    else:
        parts = urlsplit(value)
        if not parts.netloc:
            # A bare `host/owner/repo` (no scheme) is a common way to pass a repository identity.
            head, separator, tail = value.replace('\\', '/').partition('/')
            if not separator or '.' not in head:
                return None
            host, path = head, tail
        else:
            host, path = parts.hostname or '', parts.path
    path = path.strip('/')
    # A bare `host/owner/repo?access_token=…` never reaches urlsplit's netloc branch above, so the
    # query and fragment are removed here for every shape. Anything after `?`/`#` is not identity.
    path = path.split('?', 1)[0].split('#', 1)[0]
    if path.endswith('.git'):
        path = path[:-4]
    if '.' not in host or not path:
        return None
    return host.casefold() + '/' + path.casefold()


def clean_text(value):
    """Collapse whitespace runs; non-strings become an empty string."""
    if not isinstance(value, str):
        return ''
    return ' '.join(value.split())


def path_key(value):
    """Comparison key for a working directory: forward slashes, no trailing slash, case-folded."""
    if not isinstance(value, str) or not value.strip():
        return ''
    return value.strip().replace('\\', '/').rstrip('/').casefold()


def basename(path):
    """Last segment of a working directory, tolerating both separators and trailing slashes."""
    if not isinstance(path, str):
        return ''
    cleaned = path.strip().replace('\\', '/').rstrip('/')
    return cleaned.split('/')[-1] if cleaned else ''


def repo_slug(remote):
    """Query-friendly repository identifier: the last path segment of the remote.

    Empirical reason: on a real cloud library the Chinese display name (`灵犀记忆`) matched three
    concept/test pages while the repository slug matched the project page as the top hit.
    """
    cleaned = strip_credentials(remote)
    source = cleaned if cleaned else (remote if isinstance(remote, str) else '')
    source = source.strip().replace('\\', '/').rstrip('/')
    if not source:
        return ''
    last = source.split('/')[-1]
    return last[:-4] if last.endswith('.git') else last


def project_terms(value, remote=None):
    """Bounded identity spellings; no fuzzy synonyms, filesystem scans or guessed repositories."""
    text = clean_text(value)[:QUERY_MAX_CHARS]
    if strip_credentials(text) or re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', text):
        terms = [repo_slug(text)]
    else:
        terms = [clean_text(v) for v in re.split(r'\s*/\s*|\s*\|\s*|[（）()]', text) if clean_text(v)]
    terms = terms[:4]
    # This shipped file contains naming migrations only, never project goals/state or user memory.
    path = Path(__file__).resolve().parents[2] / 'integrations/project-identities.json'
    try:
        registry = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        registry = {}
    rows = registry.get('projects', []) if isinstance(registry, dict) and registry.get('schema') == 1 else []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get('aliases', []), list):
            continue
        repositories = row.get('repository_aliases', [])
        if not isinstance(repositories, list):
            continue
        if remote and strip_credentials(remote) not in {
                strip_credentials(v) for v in [row.get('repository'), *repositories] if isinstance(v, str)}:
            continue
        aliases = [row.get('name'), *row.get('aliases', [])]
        aliases = [a for a in aliases if isinstance(a, str) and 0 < len(a) <= 120]
        if any(normalize_name(term) == normalize_name(alias) for term in terms for alias in aliases):
            terms.extend(alias for alias in aliases if alias not in terms)
    unique = {}
    for term in terms:
        unique.setdefault(normalize_name(term), term)
    return list(unique.values())[:8]


def ranked_queries(project_text, cwd, request_text, remote=None):
    """Ordered query candidates: repository slug, reduced name, full name, then the request text.

    The caller caps how many may actually be sent (SEARCH_ATTEMPTS_MAX), so the ranking is the
    contract: the most discriminating identifier goes first — a repository slug is unique, while a
    display name can be a substring of unrelated pages.
    """
    ranked = []

    def add(value):
        value = clean_text(value)[:QUERY_MAX_CHARS]
        if value and value not in ranked:
            ranked.append(value)

    remote_terms = project_terms(repo_slug(remote), remote=remote) if remote else []
    terms = project_terms(project_text, remote=remote)
    for source in remote_terms + terms:
        add(reduce_name(source))
    for source in (clean_text(project_text) if len(terms) <= 1 else '', basename(cwd)):
        if source:
            add(reduce_name(source))
            add(source)
    add(request_text)
    return ranked


def clues(request='', project='', cwd='', remote=None):
    """Collect the caller-supplied hints only. Never reads the filesystem, chat or review folders."""
    text = clean_text(request)[:QUERY_MAX_CHARS]
    project_text = clean_text(project)
    if not project_text:
        quoted = re.search(r'[“「"]([^”」"\n]{2,120})[”」"]\s*(?:项目|project)', text)
        project_text = quoted.group(1) if quoted else ''
    name_key = normalize_name(project_text)
    query = project_text if name_key else text
    return {'request': text, 'project': project_text, 'name_key': name_key,
            'name_keys': list(dict.fromkeys(normalize_name(v) for v in
                         project_terms(project_text, remote=remote) +
                         (project_terms(repo_slug(remote), remote=remote) if remote else []))),
            'cwd_key': path_key(cwd), 'remote_key': strip_credentials(remote),
            'query': query[:QUERY_MAX_CHARS],
            'queries': ranked_queries(project_text, cwd, text, remote)}


def _hit_identity(hit):
    """Read one candidate page's identity hints, tolerating both page metadata shapes."""
    meta = hit.get('front_matter') if isinstance(hit.get('front_matter'), dict) else {}
    title = meta.get('title') if isinstance(meta.get('title'), str) else hit.get('title')
    name = meta.get('project') or meta.get('name') or hit.get('project_name') or title
    return (normalize_name(name if isinstance(name, str) else ''),
            path_key(meta.get('cwd') or hit.get('cwd')),
            strip_credentials(meta.get('remote') or hit.get('remote')))


def _candidate_key(hit):
    """Two pages belong to the same candidate only when their evidence says so.

    A shared name alone is not enough: pages that live in different directories and carry no
    remote/directory evidence stay separate, which is what makes same-name ambiguity visible.
    The directory is the page's own parent path, so `wiki/projects/a/x` and `wiki/projects/b/y`
    are two candidates rather than one.
    """
    _, cwd_hint, remote_hint = _hit_identity(hit)
    if remote_hint or cwd_hint:
        return remote_hint or cwd_hint
    path = str(hit.get('path', ''))
    parts = path.split('/')
    # A nested project directory may group its pages. A flat wiki/projects namespace
    # cannot group all projects together merely because they share the same parent.
    namespace_depth = 2 if path.startswith('wiki/projects/') else 1
    return '/'.join(parts[:-1]) if len(parts) > namespace_depth + 1 else path


# Cloud pages carry no structured identity fields (their metadata is title/summary/sources/related),
# so equality alone can never match them. When identity equality finds nothing, a *discriminating*
# query whose top hit sits in the project namespace is the next best evidence — and it is labelled
# as such, with the runner-ups handed back for the host to judge.
PROJECT_NAMESPACES = ('wiki/projects/', 'projects/')


def discriminating_queries(clue_set):
    """Queries strong enough to carry identity: the repository slug, or an explicit project name."""
    values = set()
    slug = repo_slug(clue_set.get('remote_key') or '')
    if slug:
        values.update(project_terms(slug, remote=clue_set.get('remote_key')))
    project = clean_text(clue_set.get('project', ''))
    if project and normalize_name(project):
        for term in project_terms(project, remote=clue_set.get('remote_key')):
            values.add(term)
            values.add(reduce_name(term))
    return {value for value in values if value}


def search_evidence(clue_set, winning_query, hits):
    """Top project-namespace hit for a discriminating query; runner-ups are reported, not chosen."""
    empty = {'state': 'none', 'candidates': [], 'runner_ups': []}
    if not winning_query or winning_query not in discriminating_queries(clue_set):
        return empty
    ranked = [row for row in (hits or []) if isinstance(row, dict)]
    ranked = sorted(ranked, key=lambda row: row.get('score') if isinstance(row.get('score'), (int, float)) else 0,
                    reverse=True)
    namespace = [row for row in ranked if str(row.get('path', '')).startswith(PROJECT_NAMESPACES)]
    if not namespace:
        return empty
    # Ranking is relevance, not identity. Require a caller-supplied distinctive identifier
    # to occur in title/path; multiple matching pages remain ambiguous until clarified.
    identifier = winning_query
    key = normalize_name(identifier)
    matching = [row for row in namespace if key and any(
        key in normalize_name(str(row.get(field) or '')) for field in ('title', 'path'))]
    if not matching:
        return empty
    candidates = [{'key': str(row.get('path')), 'title': row.get('title'), 'path': row.get('path'),
                   'updated': row.get('updated'), 'score': row.get('score'), 'evidence': 'identifier_match'}
                  for row in matching]
    return {'state': 'ok' if len(candidates) == 1 else 'project_ambiguous',
            'evidence': 'identifier_match', 'candidates': candidates,
            'runner_ups': [str(row.get('path')) for row in namespace if row not in matching]}


def explicit_scope_conflict(clue_set, metadata):
    """Only explicit scope fields may exclude a hit; titles/scores are not boundaries.

    Directory mismatch alone is insufficient (a shared project can have many checkouts).
    Every metadata shape is checked so partial front matter cannot hide a conflict.
    """
    for row in metadata:
        remote = strip_credentials(row.get('remote'))
        if remote and clue_set.get('remote_key') and remote != clue_set['remote_key']:
            return True
        for field in ('project', 'project_name', 'name'):
            name = normalize_name(row.get(field))
            if name and clue_set.get('name_keys') and name not in clue_set['name_keys']:
                return True
    return False


def select_project(clue_set, hits):
    """Return the matching project candidates, or report ambiguity instead of choosing one.

    Matching is field-wise on purpose: a name only matches a name, a directory only a directory,
    a remote only a remote. Cross-field coincidences are not evidence of the same project.
    """
    wanted = [(clue_set.get('name_key'), 0), (clue_set.get('cwd_key'), 1), (clue_set.get('remote_key'), 2)]
    wanted += [(value, 0) for value in clue_set.get('name_keys', [])]
    if not any(value for value, _ in wanted):
        return {'state': 'none', 'candidates': []}
    candidates = {}
    for item in hits or []:
        if not isinstance(item, dict):
            continue
        identity = _hit_identity(item)
        if not any(value and identity[index] == value for value, index in wanted):
            continue
        key = _candidate_key(item)
        candidates.setdefault(key, {'key': key, 'title': item.get('title'), 'path': item.get('path'),
                                    'updated': item.get('updated')})
    if not candidates:
        return {'state': 'none', 'candidates': []}
    ordered = [candidates[key] for key in sorted(candidates)]
    if len(ordered) > 1:
        return {'state': 'project_ambiguous', 'candidates': ordered}
    return {'state': 'ok', 'candidates': ordered}
