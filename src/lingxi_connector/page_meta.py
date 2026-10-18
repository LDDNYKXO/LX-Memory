"""Tags and attrs stored next to page content, readable by every client.

The upstream search API has no tag index, so metadata travels with the page body in a single
trailing comment. It survives read/update, is visible to any Agent that reads the page, and never
changes the semantic body.
"""
import json

META_OPEN = '<!-- lx-memory-meta '
META_CLOSE = ' -->'
MAX_TAGS = 10
MAX_TAG_CHARS = 32
MAX_ATTRS = 10
MAX_ATTR_KEY = 32
MAX_ATTR_VALUE = 200


def _valid_tags(tags):
    return (isinstance(tags, list) and len(tags) <= MAX_TAGS and
            all(isinstance(tag, str) and 0 < len(tag) <= MAX_TAG_CHARS and '\n' not in tag and '\r' not in tag
                for tag in tags))


def _valid_attrs(attrs):
    return (isinstance(attrs, dict) and len(attrs) <= MAX_ATTRS and
            all(isinstance(key, str) and 0 < len(key) <= MAX_ATTR_KEY and '\n' not in key and '\r' not in key
                and isinstance(value, str) and len(value) <= MAX_ATTR_VALUE for key, value in attrs.items()))


def normalize(tags, attrs):
    """Validate and canonicalize metadata; raises ValueError for invalid input."""
    tags = [] if tags is None else tags
    attrs = {} if attrs is None else attrs
    if not _valid_tags(tags) or not _valid_attrs(attrs):
        raise ValueError('invalid_metadata')
    return list(tags), dict(attrs)


def split(content):
    """Return (body, tags, attrs). Malformed or foreign trailing blocks stay in the body."""
    if not isinstance(content, str):
        return '', [], {}
    text = content.rstrip()
    if not text.endswith(META_CLOSE):
        return content, [], {}
    index = text.rfind('\n')
    line = text[index + 1:] if index >= 0 else text
    if not line.startswith(META_OPEN) or not line.endswith(META_CLOSE):
        return content, [], {}
    try:
        value = json.loads(line[len(META_OPEN):-len(META_CLOSE)])
    except (ValueError, TypeError):
        return content, [], {}
    if not isinstance(value, dict) or set(value) - {'tags', 'attrs'}:
        return content, [], {}
    tags = value.get('tags', [])
    attrs = value.get('attrs', {})
    if not _valid_tags(tags) or not _valid_attrs(attrs):
        return content, [], {}
    body = text[:index].rstrip() if index >= 0 else ''
    return body, list(tags), dict(attrs)


def with_meta(body, tags, attrs):
    """Embed metadata as one trailing comment; empty metadata removes the block."""
    body = body.rstrip() if isinstance(body, str) else ''
    tags = list(tags or [])
    attrs = dict(attrs or {})
    if not tags and not attrs:
        return body
    value = {}
    if tags:
        value['tags'] = tags
    if attrs:
        value['attrs'] = attrs
    return body + '\n\n' + META_OPEN + json.dumps(value, ensure_ascii=False, sort_keys=True) + META_CLOSE


def annotate(page):
    """Return a copy of a page dict with tags/attrs parsed from its content."""
    if not isinstance(page, dict):
        return page
    body, tags, attrs = split(page.get('content'))
    value = dict(page)
    value['tags'] = tags
    value['attrs'] = attrs
    return value
