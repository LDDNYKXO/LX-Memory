"""Search-derived browse view and local export. Cloud access stays read-only."""
import hashlib
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .cloud import ConnectorError
from .page_meta import split
from .writeback import _strict_record


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
