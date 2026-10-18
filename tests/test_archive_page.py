"""Archive tests: strict evidence structure, re-verification at commit, and guard rails."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from lingxi_connector.cloud import ConnectorError
from lingxi_connector.writeback import Writeback

TARGET = 'wiki/projects/lingxi-connector-test-1'
NEWER = 'wiki/projects/lingxi-connector-new-1'
OLD = 'wiki/projects/lingxi-connector-old-1'


def marker(**fields):
    value = {'schema': 1, 'project': 'LX Memory', 'supersedes': [],
             'as_of': '2026-10-17T00:00:00+08:00', 'record_kind': 'handoff'}
    value.update(fields)
    return '<!-- memory-project-record ' + json.dumps(value, ensure_ascii=False, sort_keys=True) + ' -->'


def record(body='disposable body', **fields):
    return marker(**fields) + '\n\n' + body


def page(path=TARGET, content='disposable body', title='probe title', **overrides):
    value = {'path': path,
             'front_matter': {'title': title, 'summary': 's', 'sources': [], 'related': []},
             'content': content, 'source': 'lingxi_cloud', 'content_scope': 'full',
             'truncated': False, 'updated': 1,
             'retrieved_at': '2026-10-17T00:00:00+00:00'}
    value.update(overrides)
    return value


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class ArchiveSession:
    epoch = 0
    account = 'account-a'
    create_contract_verified = True

    def __init__(self, content=None, path=TARGET, pages=None, search_paths=None,
                 unreadable=(), truncated=(), search_error=None):
        self.deletes = 0
        self.posts = 0
        self.puts = 0
        self.pages = dict(pages or {})
        self.pages.setdefault(path, page(path=path, content=content if content is not None else record()))
        self.search_paths = list(search_paths) if search_paths is not None else [path]
        self.unreadable = set(unreadable)
        self.truncated = set(truncated)
        self.search_error = search_error

    def write_enabled_for(self, account):
        return self.create_contract_verified

    async def identity(self):
        return self.account

    async def status(self, refresh=False):
        return {'state': 'CONNECTED', 'cloud_available': True}

    async def search(self, query, limit=5):
        if self.search_error is not None:
            raise ConnectorError(self.search_error)
        return {'source': 'lingxi_cloud', 'total': len(self.search_paths),
                'results': [{'path': p} for p in self.search_paths]}

    async def read(self, path, offset=0, max_chars=20000):
        if path in self.unreadable:
            raise ConnectorError('UPSTREAM_INCOMPLETE')
        if path in self.truncated:
            return dict(self.pages[path], content_scope='partial', truncated=True)
        if path not in self.pages:
            raise ConnectorError('PAGE_NOT_FOUND')
        return dict(self.pages[path])

    async def delete_page(self, path, expected_account=None, expected_epoch=None):
        self.deletes += 1
        if path not in self.pages:
            raise ConnectorError('PAGE_NOT_FOUND')
        del self.pages[path]
        return {'path': path}

    async def create_page(self, *args, **kwargs):
        self.posts += 1
        return {}

    async def replace_page(self, *args, **kwargs):
        self.puts += 1
        return {}


class ArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def writeback(self, session):
        return Writeback(session, Path(self.tmp.name), clock=self.clock)

    # ---- valid evidence -------------------------------------------------

    async def test_preview_shows_body_and_deletes_nothing(self):
        session = ArchiveSession(content=record('disposable body', record_kind='test'))
        w = self.writeback(session)
        p = await w.prepare_archive(TARGET)
        self.assertEqual(p['operation'], 'delete')
        self.assertIn('disposable body', p['deleting']['content'])
        self.assertEqual(p['evidence']['kind'], 'test_record')
        self.assertIn('content_sha256', p['evidence'])
        self.assertEqual(session.deletes, 0)

    async def test_commit_deletes_and_verifies_absence(self):
        session = ArchiveSession(content=record(record_kind='test'))
        w = self.writeback(session)
        p = await w.prepare_archive(TARGET)
        r = await w.commit(p['proposal_id'])
        self.assertEqual(r['state'], 'deleted_verified')
        self.assertEqual(session.deletes, 1)

    async def test_history_record_is_deletable(self):
        session = ArchiveSession(content=record(record_kind='history'))
        p = await self.writeback(session).prepare_archive(TARGET)
        self.assertEqual(p['evidence']['kind'], 'history_record')

    async def test_page_superseded_by_strict_project_record(self):
        newer = page(path=NEWER, content=record('newer body', record_kind='handoff', supersedes=[TARGET]))
        session = ArchiveSession(content='plain body', pages={NEWER: newer},
                                 search_paths=[TARGET, NEWER])
        p = await self.writeback(session).prepare_archive(TARGET)
        self.assertEqual(p['evidence']['kind'], 'superseded_by')
        self.assertEqual(p['evidence']['by'], NEWER)

    async def test_explicit_superseded_by_is_verified_without_search(self):
        newer = page(path=NEWER, content=record(record_kind='handoff', supersedes=[TARGET]))
        session = ArchiveSession(content='plain body', pages={NEWER: newer}, search_paths=[])
        p = await self.writeback(session).prepare_archive(TARGET, superseded_by=NEWER)
        self.assertEqual(p['evidence']['kind'], 'superseded_by')

    # ---- R01: structure and type checks ---------------------------------

    async def test_string_supersedes_is_not_evidence(self):
        for value in (TARGET, {'path': TARGET}, 7, None, [TARGET, 7], [TARGET, TARGET]):
            newer = page(path=NEWER, content=marker(supersedes=value))
            session = ArchiveSession(content='plain body', pages={NEWER: newer}, search_paths=[])
            with self.assertRaises(ConnectorError) as raised:
                await self.writeback(session).prepare_archive(TARGET, superseded_by=NEWER)
            self.assertEqual(raised.exception.code, 'TARGET_NOT_SUPERSEDED')

    async def test_substring_or_prefix_path_is_not_exact_membership(self):
        for wrong in (TARGET + '-suffix', TARGET[:-1], 'wiki/projects/lingxi-connector-test-1x'):
            newer = page(path=NEWER, content=marker(supersedes=[wrong]))
            session = ArchiveSession(content='plain body', pages={NEWER: newer}, search_paths=[])
            with self.assertRaises(ConnectorError) as raised:
                await self.writeback(session).prepare_archive(TARGET, superseded_by=NEWER)
            self.assertEqual(raised.exception.code, 'TARGET_NOT_SUPERSEDED')

    async def test_invalid_record_structure_is_not_evidence(self):
        variants = [
            marker(record_kind='project'),
            marker(schema=2),
            marker(project=''),
            marker(supersedes=['projects/not-wiki']),
            marker(supersedes=['wiki/projects/../etc']),
            marker(supersedes=[TARGET, TARGET]),
            marker(supersedes=[TARGET] * 5),
            marker(as_of='2026-10-17T00:00:00'),
        ]
        for content in variants:
            newer = page(path=NEWER, content=content)
            session = ArchiveSession(content='plain body', pages={NEWER: newer}, search_paths=[])
            with self.assertRaises(ConnectorError) as raised:
                await self.writeback(session).prepare_archive(TARGET, superseded_by=NEWER)
            self.assertEqual(raised.exception.code, 'TARGET_NOT_SUPERSEDED', content)

    async def test_self_declared_marker_without_structure_is_not_deletable(self):
        lenient = '<!-- memory-project-record {"record_kind":"test","as_of":"2026-10-17T00:00:00+08:00"} -->\n\nbody'
        session = ArchiveSession(content=lenient)
        with self.assertRaises(ConnectorError) as raised:
            await self.writeback(session).prepare_archive(TARGET)
        self.assertEqual(raised.exception.code, 'TARGET_NOT_SUPERSEDED')

    async def test_evidence_must_be_a_project_page(self):
        for candidate in ('wiki/concepts/lingxi-connector-new-1', 'wiki/entities/lingxi-connector-new-1'):
            newer = page(path=candidate, content=marker(supersedes=[TARGET]))
            session = ArchiveSession(content='plain body', pages={candidate: newer}, search_paths=[])
            with self.assertRaises(ConnectorError) as raised:
                await self.writeback(session).prepare_archive(TARGET, superseded_by=candidate)
            self.assertEqual(raised.exception.code, 'INVALID_INPUT')

    async def test_evidence_project_must_match_target_project(self):
        newer = page(path=NEWER, content=marker(project='Other Project', supersedes=[TARGET]))
        session = ArchiveSession(content=marker(record_kind='handoff'), pages={NEWER: newer}, search_paths=[])
        with self.assertRaises(ConnectorError) as raised:
            await self.writeback(session).prepare_archive(TARGET, superseded_by=NEWER)
        self.assertEqual(raised.exception.code, 'TARGET_NOT_SUPERSEDED')

    async def test_unreadable_or_partial_evidence_is_refused(self):
        newer = page(path=NEWER, content=marker(supersedes=[TARGET]))
        for kwargs, code in (({'unreadable': [NEWER]}, 'UPSTREAM_INCOMPLETE'),
                             ({'truncated': [NEWER]}, 'TARGET_READ_INCOMPLETE')):
            session = ArchiveSession(content='plain body', pages={NEWER: newer}, search_paths=[], **kwargs)
            with self.assertRaises(ConnectorError) as raised:
                await self.writeback(session).prepare_archive(TARGET, superseded_by=NEWER)
            self.assertEqual(raised.exception.code, code)

    async def test_non_cloud_evidence_read_is_refused(self):
        newer = page(path=NEWER, content=marker(supersedes=[TARGET]), source='local_cache')
        session = ArchiveSession(content='plain body', pages={NEWER: newer}, search_paths=[])
        with self.assertRaises(ConnectorError) as raised:
            await self.writeback(session).prepare_archive(TARGET, superseded_by=NEWER)
        self.assertEqual(raised.exception.code, 'UPSTREAM_INCOMPATIBLE')

    async def test_search_failure_does_not_grant_disposability(self):
        session = ArchiveSession(content='plain body', search_error='UPSTREAM_INCOMPLETE')
        with self.assertRaises(ConnectorError) as raised:
            await self.writeback(session).prepare_archive(TARGET)
        self.assertEqual(raised.exception.code, 'TARGET_NOT_SUPERSEDED')

    async def test_protected_scopes_are_refused(self):
        for path in ('core/核心记忆', 'wiki/profile/lingxi-connector-abc'):
            session = ArchiveSession(path=path, content=record(record_kind='test'))
            with self.assertRaises(ConnectorError) as raised:
                await self.writeback(session).prepare_archive(path)
            self.assertEqual(raised.exception.code, 'PROTECTED_TARGET')

    # ---- R02: re-verify evidence at commit ------------------------------

    async def _prepared(self, content='plain body', **kwargs):
        newer = page(path=NEWER, content=record(record_kind='handoff', supersedes=[TARGET]))
        session = ArchiveSession(content=content, pages={NEWER: newer}, search_paths=[], **kwargs)
        w = self.writeback(session)
        proposal = await w.prepare_archive(TARGET, superseded_by=NEWER)
        return session, w, proposal

    async def test_commit_refuses_when_evidence_page_disappeared(self):
        session, w, p = await self._prepared()
        del session.pages[NEWER]
        r = await w.commit(p['proposal_id'])
        self.assertEqual((r['state'], r['error']), ('rejected', 'EVIDENCE_MISSING'))
        self.assertEqual(session.deletes, 0)

    async def test_commit_refuses_when_evidence_changed(self):
        session, w, p = await self._prepared()
        session.pages[NEWER]['content'] = record(record_kind='handoff', supersedes=[TARGET]) + '\nchanged'
        r = await w.commit(p['proposal_id'])
        self.assertEqual((r['state'], r['error']), ('rejected', 'EVIDENCE_CHANGED'))
        self.assertEqual(session.deletes, 0)

    async def test_commit_refuses_when_evidence_revoked(self):
        session, w, p = await self._prepared()
        session.pages[NEWER]['content'] = record(record_kind='handoff', supersedes=[OLD])
        r = await w.commit(p['proposal_id'])
        self.assertEqual((r['state'], r['error']), ('rejected', 'EVIDENCE_CHANGED'))
        self.assertEqual(session.deletes, 0)

    async def test_commit_refuses_when_evidence_unreadable(self):
        session, w, p = await self._prepared()
        session.unreadable.add(NEWER)
        r = await w.commit(p['proposal_id'])
        self.assertEqual((r['state'], r['error']), ('rejected', 'EVIDENCE_UNREADABLE'))
        self.assertEqual(session.deletes, 0)

    async def test_commit_refuses_when_target_changed(self):
        session, w, p = await self._prepared()
        session.pages[TARGET]['content'] = record(record_kind='test') + 'x'
        r = await w.commit(p['proposal_id'])
        self.assertEqual((r['state'], r['error']), ('rejected', 'TARGET_CHANGED'))
        self.assertEqual(session.deletes, 0)

    async def test_commit_refuses_on_account_epoch_or_expiry(self):
        session, w, p = await self._prepared()
        session.account = 'account-b'
        self.assertEqual((await w.commit(p['proposal_id']))['error'], 'ACCOUNT_CHANGED')
        self.assertEqual(session.deletes, 0)

        session, w, p = await self._prepared()
        session.epoch = 1
        self.assertEqual((await w.commit(p['proposal_id']))['error'], 'ACCOUNT_CHANGED')

        session, w, p = await self._prepared()
        self.clock.now += 601
        self.assertEqual((await w.commit(p['proposal_id']))['error'], 'PROPOSAL_EXPIRED')
        self.assertEqual(session.deletes, 0)

    async def test_concurrent_commit_deletes_at_most_once(self):
        session, w, p = await self._prepared()
        first, second = await asyncio.gather(w.commit(p['proposal_id']), w.commit(p['proposal_id']))
        self.assertEqual(first, second)
        self.assertEqual(session.deletes, 1)

    async def test_valid_evidence_still_deletes(self):
        session, w, p = await self._prepared()
        r = await w.commit(p['proposal_id'])
        self.assertEqual(r['state'], 'deleted_verified')
        self.assertEqual(session.deletes, 1)


if __name__ == '__main__':
    unittest.main()
