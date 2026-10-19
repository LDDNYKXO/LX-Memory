"""Search-derived browse view, local export and backup. Cloud access stays read-only."""
import hashlib
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .cloud import ConnectorError
from .page_meta import split
from .writeback import _strict_record

CURATED_QUERIES = ('用户画像 协作偏好', 'LX Memory 交接', '经验 教训')
MAX_BACKUP_ENTRIES = 10
MAX_VERIFY_ENTRIES = 100
MAX_SNAPSHOT_BYTES = 5_000_000


async def _read_full(session, path):
    page = await session.read(path, 0, 20000)
    if (not isinstance(page, dict) or page.get('source') != 'lingxi_cloud'
            or page.get('content_scope') != 'full' or page.get('truncated') is True):
        raise ConnectorError('TARGET_READ_INCOMPLETE')
    return page


async def browse(session, query, limit=10, tag=''):
    """Compact view of search hits enriched with record kind, as_of, tags and attrs."""
    if not isinstance(query, str) or not query.strip():
        raise ConnectorError('INVALID_INPUT')
    if not isinstance(limit, int) or not 1 <= limit <= 20:
        raise ConnectorError('INVALID_INPUT')
    if not isinstance(tag, str) or len(tag) > 32 or '\n' in tag or '\r' in tag:
        raise ConnectorError('INVALID_INPUT')
    matches = await session.search(query, 20)
    hits = [row for row in ((matches or {}).get('results') or []) if isinstance(row, dict)]
    rows, reads = [], 0
    for hit in hits:
        if len(rows) >= limit or reads >= 20:
            break
        path = hit.get('path')
        if not isinstance(path, str) or not path.startswith('wiki/'):
            continue
        try:
            page = await _read_full(session, path)
        except ConnectorError:
            continue
        reads += 1
        body, tags, attrs = split(page.get('content'))
        if tag and tag not in tags:
            continue
        marker = _strict_record(page.get('content'))
        front = page.get('front_matter') if isinstance(page.get('front_matter'), dict) else {}
        rows.append({'path': path,
                     'title': front.get('title') or hit.get('title'),
                     'summary': front.get('summary') or hit.get('summary'),
                     'updated': page.get('updated') or hit.get('updated'),
                     'score': hit.get('score'),
                     'record_kind': marker[0].get('record_kind') if marker else None,
                     'as_of': marker[0].get('as_of') if marker else None,
                     'tags': tags, 'attrs': attrs,
                     'chars': len(page.get('content') or '')})
    return {'query': query, 'tag': tag or None, 'rows': rows,
            'candidates': len(hits), 'reads': reads}


def _export_target(output, directory):
    if output:
        target = Path(output).expanduser()
        if not target.is_absolute():
            raise ConnectorError('INVALID_INPUT')
        target = target.resolve()
        allowed = [Path.home().resolve(), Path(tempfile.gettempdir()).resolve(), Path(directory).resolve()]
        if not any(target == base or base in target.parents for base in allowed):
            raise ConnectorError('INVALID_INPUT')
    else:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        target = (Path(directory).resolve() / 'exports' / ('export-' + stamp + '.json'))
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


async def export(session, query='', paths=None, limit=10, output='', directory=None):
    """Write a local JSON snapshot of full pages; the cloud side is read-only."""
    if directory is None:
        raise ConnectorError('INVALID_INPUT')
    if paths is not None:
        if (not isinstance(paths, list) or not 1 <= len(paths) <= 50 or
                any(not isinstance(path, str) or not path.startswith(('wiki/', 'core/')) for path in paths)):
            raise ConnectorError('INVALID_INPUT')
        targets = list(paths)
    else:
        if not isinstance(query, str) or not query.strip():
            raise ConnectorError('INVALID_INPUT')
        if not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ConnectorError('INVALID_INPUT')
        matches = await session.search(query, min(limit, 20))
        targets = [row.get('path') for row in ((matches or {}).get('results') or [])
                   if isinstance(row, dict) and isinstance(row.get('path'), str)
                   and row['path'].startswith(('wiki/', 'core/'))][:limit]
    entries, skipped = [], []
    for path in targets:
        try:
            page = await _read_full(session, path)
        except ConnectorError as error:
            skipped.append({'path': path, 'error': error.code})
            continue
        content = page.get('content') or ''
        body, tags, attrs = split(content)
        entries.append({'path': path,
                        'front_matter': page.get('front_matter') or {},
                        'content': content,
                        'tags': tags, 'attrs': attrs,
                        'sha256': hashlib.sha256(content.encode('utf-8')).hexdigest()})
    target = _export_target(output, directory)
    payload = {'schema': 'lingxi-memory/export/v1',
               'generated_at': datetime.now(timezone.utc).isoformat(),
               'query': None if paths is not None else query,
               'entries': entries, 'skipped': skipped}
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'output': str(target), 'entries': len(entries), 'skipped': skipped,
            'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
            'paths': [entry['path'] for entry in entries]}


def _allowed_root(path, directory):
    allowed = [Path.home().resolve(), Path(tempfile.gettempdir()).resolve(), Path(directory).resolve()]
    return any(path == base or base in path.parents for base in allowed)


async def _curated_targets(session, query):
    targets = ['core/核心记忆']
    queries = [query] if query else list(CURATED_QUERIES)
    for text in queries:
        try:
            matches = await session.search(text, 5)
        except ConnectorError:
            continue
        for row in ((matches or {}).get('results') or []):
            path = row.get('path') if isinstance(row, dict) else None
            if isinstance(path, str) and path.startswith(('wiki/', 'core/')) and path not in targets:
                targets.append(path)
            if len(targets) >= MAX_BACKUP_ENTRIES:
                return targets[:MAX_BACKUP_ENTRIES]
    return targets[:MAX_BACKUP_ENTRIES]


async def backup(session, query='', paths=None, output_dir='', keep=5, directory=None):
    """Timestamped local snapshot plus retention pruning. Cloud side stays read-only."""
    if directory is None:
        raise ConnectorError('INVALID_INPUT')
    if not isinstance(keep, int) or not 1 <= keep <= 50:
        raise ConnectorError('INVALID_INPUT')
    if paths is not None:
        if (not isinstance(paths, list) or not 1 <= len(paths) <= MAX_BACKUP_ENTRIES or
                any(not isinstance(path, str) or not path.startswith(('wiki/', 'core/')) for path in paths)):
            raise ConnectorError('INVALID_INPUT')
        targets = list(paths)
    else:
        if not isinstance(query, str):
            raise ConnectorError('INVALID_INPUT')
        targets = await _curated_targets(session, query)
    if not targets:
        raise ConnectorError('INVALID_INPUT')
    root = Path(output_dir).expanduser() if output_dir else Path(directory) / 'backups'
    if not root.is_absolute():
        raise ConnectorError('INVALID_INPUT')
    root = root.resolve()
    if not _allowed_root(root, directory):
        raise ConnectorError('INVALID_INPUT')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target_dir = root / stamp
    target_dir.mkdir(parents=True, exist_ok=False)
    try:
        written = await export(session, paths=targets, output=str(target_dir / 'snapshot.json'),
                               directory=directory)
    except Exception:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise
    keep_dirs = sorted((path for path in root.iterdir() if path.is_dir()), key=lambda path: path.name,
                       reverse=True)
    pruned = []
    for old in keep_dirs[keep:]:
        shutil.rmtree(old, ignore_errors=True)
        pruned.append(str(old))
    return {'backup': str(target_dir), 'snapshot': written['output'],
            'entries': written['entries'], 'skipped': written['skipped'],
            'sha256': written['sha256'], 'pruned': pruned, 'keep': keep,
            'created_at': datetime.now(timezone.utc).isoformat()}


async def verify_backup(session, snapshot, directory=None):
    """Compare a local snapshot against current cloud pages; read-only on both sides."""
    if directory is None:
        raise ConnectorError('INVALID_INPUT')
    if not isinstance(snapshot, str) or not snapshot:
        raise ConnectorError('INVALID_INPUT')
    path = Path(snapshot).expanduser()
    if not path.is_absolute():
        raise ConnectorError('INVALID_INPUT')
    path = path.resolve()
    backups_root = (Path(directory).resolve() / 'backups')
    allowed = [backups_root, Path.home().resolve(), Path(tempfile.gettempdir()).resolve()]
    if not any(path == base or base in path.parents for base in allowed):
        raise ConnectorError('INVALID_INPUT')
    if not path.is_file() or path.stat().st_size > MAX_SNAPSHOT_BYTES:
        raise ConnectorError('INVALID_INPUT')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise ConnectorError('INVALID_INPUT') from None
    entries = data.get('entries') if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ConnectorError('INVALID_INPUT')
    checked = entries[:MAX_VERIFY_ENTRIES]
    ok, drifted, missing, unreadable = 0, [], [], []
    for entry in checked:
        if not isinstance(entry, dict):
            continue
        target = entry.get('path')
        expected = entry.get('sha256')
        if not isinstance(target, str) or not isinstance(expected, str):
            continue
        try:
            page = await _read_full(session, target)
        except ConnectorError as error:
            if error.code == 'PAGE_NOT_FOUND':
                missing.append(target)
            else:
                unreadable.append({'path': target, 'error': error.code})
            continue
        actual = hashlib.sha256((page.get('content') or '').encode('utf-8')).hexdigest()
        if actual == expected:
            ok += 1
        else:
            drifted.append({'path': target, 'expected': expected, 'actual': actual})
    return {'snapshot': str(path), 'generated_at': data.get('generated_at'),
            'checked': len(checked), 'ok': ok, 'drifted': drifted,
            'missing': missing, 'unreadable': unreadable}
