import json
from datetime import datetime, timezone
from urllib.parse import quote, urlencode, urlsplit

ORIGIN = 'https://lingxi.kdocs.cn'
PREFIX = ORIGIN + '/api/public/v1/agents/default/memory/'
IDENTITY_URL = 'https://account.kdocs.cn/api/v3/islogin'

def identity_result(status, body):
    if status != 200 or not isinstance(body, dict) or body.get('result') != 'ok':
        raise ConnectorError('AUTH_REQUIRED')
    user = body.get('userid')
    if user is None and isinstance(body.get('data'), dict):
        user = body['data'].get('userid')
    if type(user) not in (str, int) or not str(user).strip() or str(user) == '0':
        raise ConnectorError('ACCOUNT_UNVERIFIED')
    return str(user)

class ConnectorError(Exception):
    def __init__(self, code, message=''):
        self.code = code
        super().__init__(message or code)

def read_url(path):
    if not isinstance(path, str) or len(path) > 1024 or any(c in path for c in '\\?#%:\x00') or any(ord(c) < 32 for c in path):
        raise ConnectorError('INVALID_INPUT')
    parts = path.split('/')
    if len(parts) < 2 or parts[0] not in ('core', 'wiki') or any(p in ('', '.', '..') for p in parts):
        raise ConnectorError('INVALID_INPUT')
    return PREFIX + 'pages/' + '/'.join(quote(p, safe='') for p in parts)

def search_url(query, limit=5):
    if not isinstance(query, str) or not query.strip() or len(query) > 2000 or type(limit) is not int or not 1 <= limit <= 20:
        raise ConnectorError('INVALID_INPUT')
    return PREFIX + 'search?' + urlencode({'query': query, 'max_results': limit})

def decode_response(status, url, body):
    if status == 404 and url.startswith(PREFIX + 'pages/'):
        raise ConnectorError('PAGE_NOT_FOUND')
    if status in (401, 302, 303, 307, 308) or urlsplit(url).hostname in ('account.wps.cn', 'account.kdocs.cn'):
        raise ConnectorError('AUTH_REQUIRED')
    if status == 403:
        raise ConnectorError('PERMISSION_DENIED')
    if status == 429:
        raise ConnectorError('RATE_LIMITED')
    if status >= 500:
        raise ConnectorError('NETWORK_ERROR')
    if status != 200 or not (url == ORIGIN or url.startswith(ORIGIN + '/')):
        raise ConnectorError('UPSTREAM_INCOMPATIBLE')
    try:
        obj = json.loads(body)
    except (ValueError, TypeError):
        raise ConnectorError('UPSTREAM_INCOMPATIBLE') from None
    if not isinstance(obj, dict) or obj.get('result') != 'ok' or type(obj.get('v7code')) is not int or obj['v7code'] != 0 or not isinstance(obj.get('data'), dict):
        raise ConnectorError('UPSTREAM_INCOMPATIBLE')
    return obj['data']

def stamp():
    return {'source': 'lingxi_cloud', 'retrieved_at': datetime.now(timezone.utc).isoformat()}

def validate_page(data):
    if not isinstance(data, dict) or not isinstance(data.get('path'), str) or not isinstance(data.get('front_matter'), dict) or not isinstance(data.get('content'), str) or 'updated' not in data:
        raise ConnectorError('UPSTREAM_INCOMPATIBLE')
    # Return identity untouched, but reject a response outside our known namespaces.
    try:
        read_url(data['path'])
    except ConnectorError:
        raise ConnectorError('UPSTREAM_INCOMPATIBLE') from None

def read_result(data, offset=0, max_chars=8000):
    if type(offset) is not int or offset < 0 or type(max_chars) is not int or not 1 <= max_chars <= 20000:
        raise ConnectorError('INVALID_INPUT')
    validate_page(data)
    content = data['content']
    end = min(offset + max_chars, len(content))
    full = offset == 0 and end == len(content)
    return {**stamp(), 'path': data['path'], 'front_matter': data['front_matter'], 'content': content[offset:end], 'updated': data['updated'], 'content_total_chars': len(content), 'offset': offset, 'truncated': not full, 'next_offset': end if end < len(content) else None, 'content_scope': 'full' if full else 'partial'}

def search_result(data):
    if not isinstance(data, dict) or type(data.get('total')) is not int or data['total'] < 0 or not isinstance(data.get('results'), list):
        raise ConnectorError('UPSTREAM_INCOMPATIBLE')
    rows = []
    for row in data['results']:
        validate_page(row)
        fm = row['front_matter']
        rows.append({'path': row['path'], 'title': fm.get('title'), 'summary': fm.get('summary'), 'snippet': row.get('snippet'), 'score': row.get('score'), 'updated': row['updated']})
    return {**stamp(), 'total': data['total'], 'results': rows}
