"""Bounded volatile snapshots. Reading an audit never invokes the cloud."""
import json
import secrets
import time
from collections import OrderedDict
from datetime import datetime, timezone

from .cloud import ConnectorError
from .context_output import output_chars

AUDIT_TTL_SECONDS = 900
AUDIT_MAX_SNAPSHOTS = 8
AUDIT_MAX_CHARS = 2_000_000


class AuditSnapshots:
    def __init__(self):
        self.entries = OrderedDict()

    def _prune(self):
        now = time.monotonic()
        for key in list(self.entries):
            if now >= self.entries[key]['expires']:
                del self.entries[key]

    def store(self, raw, scope):
        self._prune()
        sections = []
        for name, value in raw.items():
            if not name.startswith('_'):
                sections.append((name, json.dumps(value, ensure_ascii=False)))
        chars = sum(len(text) for _, text in sections)
        if chars > AUDIT_MAX_CHARS:
            return {'status': 'unavailable', 'reason': 'snapshot_capacity_exceeded'}
        while self.entries and (len(self.entries) >= AUDIT_MAX_SNAPSHOTS or
              sum(x['chars'] for x in self.entries.values()) + chars > AUDIT_MAX_CHARS):
            self.entries.popitem(last=False)
        identifier = secrets.token_hex(16)
        self.entries[identifier] = {'scope': scope, 'sections': sections, 'chars': chars,
                                    'expires': time.monotonic() + AUDIT_TTL_SECONDS, 'cursors': {}}
        return {'status': 'available', 'snapshot_id': identifier,
                'expires_at': datetime.fromtimestamp(time.time() + AUDIT_TTL_SECONDS, timezone.utc).isoformat()}

    def available(self, reference, scope):
        self._prune()
        entry = self.entries.get(reference.get('snapshot_id'))
        return entry is not None and entry['scope'] == scope

    def read(self, identifier, scope, cursor='', limit=8000):
        if not isinstance(identifier, str) or not identifier or not isinstance(cursor, str):
            raise ConnectorError('INVALID_INPUT')
        self._prune()
        entry = self.entries.get(identifier)
        if entry is None:
            raise ConnectorError('AUDIT_SNAPSHOT_EXPIRED')
        if scope != entry['scope']:
            raise ConnectorError('AUDIT_ACCESS_DENIED')
        position = (0, 0) if not cursor else entry['cursors'].get(cursor)
        if position is None:
            raise ConnectorError('INVALID_AUDIT_CURSOR')
        section_index, offset = position
        name, text = entry['sections'][section_index]
        # One section fragment per page; a cursor is bound to this snapshot and offset.
        token = secrets.token_hex(16)
        def page(end):
            more = end < len(text) or section_index + 1 < len(entry['sections'])
            return {'response_schema': 2, 'view': 'audit', 'snapshot_id': identifier,
                    'sections': [{'name': name, 'offset': offset, 'content': text[offset:end]}],
                    'next_cursor': token if more else None, 'has_more': more}
        low, high = offset, min(len(text), offset + limit)
        while low < high:
            mid = (low + high + 1) // 2
            if output_chars(page(mid)) <= limit: low = mid
            else: high = mid - 1
        if low == offset and len(text) > offset:
            raise ConnectorError('AUDIT_BUDGET_TOO_SMALL')
        result = page(low)
        # Only a successful page read makes a snapshot "recently used"; a forged/expired cursor
        # must not extend its life.
        self.entries.move_to_end(identifier)
        if result['has_more']:
            next_position = (section_index, low) if low < len(text) else (section_index + 1, 0)
            # Stable cursor on repeated reads, avoiding unbounded growth on replay.
            existing = next((k for k, v in entry['cursors'].items() if v == next_position), None)
            if existing:
                result['next_cursor'] = existing
            else:
                entry['cursors'][token] = next_position
        return result
