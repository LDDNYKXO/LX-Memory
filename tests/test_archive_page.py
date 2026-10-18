"""Archive (permanent delete) tests: preview, evidence rules, guard rails, and read-back."""
import json
import tempfile
import unittest
from pathlib import Path

from lingxi_connector.cloud import ConnectorError
from lingxi_connector.writeback import Writeback

TARGET = 'wiki/projects/lingxi-connector-test-1'
NEWER = 'wiki/projects/lingxi-connector-new-1'


def record(body='disposable body', record_kind='test', supersedes=None):
    """Page content whose first line is a project record marker."""
    marker = {'schema': 1, 'project': 'LX Memory', 'record_kind': record_kind,
              'as_of': '2026-10-18T00:00:00+08:00'}
    if supersedes is not None:
        marker['supersedes'] = list(supersedes)
    return '<!-- memory-project-record ' + json.dumps(marker, ensure_ascii=False, sort_keys=True) + ' -->\n\n' + body


def page(path=TARGET, content='disposable body', title='probe title', **overrides):
    value = {'path': path,
             'front_matter': {'title': title, 'summary': 's'},
             'content': content, 'source': 'lingxi_cloud', 'content_scope': 'full',
             'truncated': False, 'updated': 1,
             'retrieved_at': '2026-10-17T00:00:00+00:00'}
    value.update(overrides)
    return value


class ArchiveSession:
    epoch = 0
    account = 'account-a'
    create_contract_verified = True

    def __init__(self, content='disposable body', path=TARGET, pages=None, search_paths=None,
                 unreadable=(), truncated=(), search_error=None):
        self.deletes = 0
        self.posts = 0
        self.puts = 0
        self.pages = dict(pages or {})
        self.pages.setdefault(path, page(path=path, content=content))
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

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def writeback(self, session):
        return Writeback(session, Path(self.tmp.name))

    async def test_preview_shows_body_and_deletes_nothing(self):
        session = ArchiveSession(content=record('disposable body'))
        w = self.writeback(session)
        p = await w.prepare_archive(TARGET)
        self.assertEqual(p['operation'], 'delete')
        self.assertEqual(p['path'], TARGET)
        self.assertIn('disposable body', p['deleting']['content'])
        self.assertEqual(p['deleting']['title'], 'probe title')
        self.assertEqual(p['evidence']['kind'], 'test_record')
        self.assertEqual(session.deletes, 0)

    async def test_commit_deletes_and_verifies_absence(self):
        session = ArchiveSession(content=record())
        w = self.writeback(session)
        p = await w.prepare_archive(TARGET)
        r = await w.commit(p['proposal_id'])
        self.assertEqual(r['state'], 'deleted_verified')
        self.assertEqual(session.deletes, 1)
        self.assertNotIn(TARGET, session.pages)

    async def test_history_record_is_disposable(self):
        session = ArchiveSession(content=record(record_kind='history'))
        w = self.writeback(session)
        p = await w.prepare_archive(TARGET)
        self.assertEqual(p['evidence']['kind'], 'history_record')

    async def test_page_superseded_by_another_record_is_disposable(self):
        newer = page(path=NEWER, content=record('newer body', record_kind='project',
                                                supersedes=[TARGET]))
        session = ArchiveSession(content='plain body', pages={NEWER: newer},
                                 search_paths=[TARGET, NEWER])
        w = self.writeback(session)
        p = await w.prepare_archive(TARGET)
        self.assertEqual(p['evidence']['kind'], 'superseded_by')
        self.assertEqual(p['evidence']['by'], NEWER)

    async def test_explicit_superseded_by_is_verified_without_search(self):
        newer = page(path=NEWER, content=record('newer body', record_kind='project',
                                                supersedes=[TARGET]))
        session = ArchiveSession(content='plain body', pages={NEWER: newer}, search_paths=[])
        w = self.writeback(session)
        p = await w.prepare_archive(TARGET, superseded_by=NEWER)
        self.assertEqual(p['evidence']['kind'], 'superseded_by')
        self.assertEqual(p['evidence']['by'], NEWER)

    async def test_explicit_evidence_must_list_the_target(self):
        newer = page(path=NEWER, content=record('newer body', record_kind='project',
                                                supersedes=['wiki/projects/other']))
        session = ArchiveSession(content='plain body', pages={NEWER: newer}, search_paths=[])
        w = self.writeback(session)
        with self.assertRaises(ConnectorError) as raised:
            await w.prepare_archive(TARGET, superseded_by=NEWER)
        self.assertEqual(raised.exception.code, 'TARGET_NOT_SUPERSEDED')

    async def test_explicit_evidence_must_be_wiki_and_not_the_target(self):
        newer = page(path=NEWER, content=record('newer body', record_kind='project',
                                                supersedes=[TARGET]))
        session = ArchiveSession(content='plain body', pages={NEWER: newer}, search_paths=[])
        w = self.writeback(session)
        for bad in ('not/a/wiki/path', TARGET):
            with self.assertRaises(ConnectorError) as raised:
                await w.prepare_archive(TARGET, superseded_by=bad)
            self.assertEqual(raised.exception.code, 'INVALID_INPUT')
        with self.assertRaises(ConnectorError) as raised:
            await w.prepare_archive(TARGET, superseded_by='core/核心记忆')
        self.assertEqual(raised.exception.code, 'PROTECTED_TARGET')

    async def test_explicit_evidence_page_must_exist(self):
        session = ArchiveSession(content='plain body', search_paths=[])
        w = self.writeback(session)
        with self.assertRaises(ConnectorError) as raised:
            await w.prepare_archive(TARGET, superseded_by=NEWER)
        self.assertEqual(raised.exception.code, 'PAGE_NOT_FOUND')

    async def test_non_disposable_page_is_refused(self):
        session = ArchiveSession(content='plain body')
        w = self.writeback(session)
        with self.assertRaises(ConnectorError) as raised:
            await w.prepare_archive(TARGET)
        self.assertEqual(raised.exception.code, 'TARGET_NOT_SUPERSEDED')
        self.assertEqual(session.deletes, 0)

    async def test_live_project_handoff_is_never_disposable(self):
        # Every page this product writes starts with `lingxi-connector-`, so that prefix must not
        # count as proof of disposability: a live handoff has no test/history marker and no
        # record that supersedes it.
        handoff = 'wiki/projects/lingxi-connector-2513728d6f3b625f75ccb8d107646f8c'
        session = ArchiveSession(path=handoff, content=record('live handoff', record_kind='project'))
        w = self.writeback(session)
        with self.assertRaises(ConnectorError) as raised:
            await w.prepare_archive(handoff)
        self.assertEqual(raised.exception.code, 'TARGET_NOT_SUPERSEDED')
        self.assertEqual(session.deletes, 0)

    async def test_marker_without_test_or_history_kind_is_not_disposable(self):
        session = ArchiveSession(content=record('project page', record_kind='project'))
        w = self.writeback(session)
        with self.assertRaises(ConnectorError) as raised:
            await w.prepare_archive(TARGET)
        self.assertEqual(raised.exception.code, 'TARGET_NOT_SUPERSEDED')

    async def test_unreadable_superseding_page_does_not_grant_disposability(self):
        newer = page(path=NEWER, content=record(supersedes=[TARGET]))
        session = ArchiveSession(content='plain body', pages={NEWER: newer},
                                 search_paths=[TARGET, NEWER], unreadable=[NEWER])
        w = self.writeback(session)
        with self.assertRaises(ConnectorError) as raised:
            await w.prepare_archive(TARGET)
        self.assertEqual(raised.exception.code, 'TARGET_NOT_SUPERSEDED')

    async def test_partial_superseding_page_does_not_grant_disposability(self):
        newer = page(path=NEWER, content=record(supersedes=[TARGET]))
        session = ArchiveSession(content='plain body', pages={NEWER: newer},
                                 search_paths=[TARGET, NEWER], truncated=[NEWER])
        w = self.writeback(session)
        with self.assertRaises(ConnectorError) as raised:
            await w.prepare_archive(TARGET)
        self.assertEqual(raised.exception.code, 'TARGET_NOT_SUPERSEDED')

    async def test_search_failure_does_not_grant_disposability(self):
        session = ArchiveSession(content='plain body', search_error='UPSTREAM_INCOMPLETE')
        w = self.writeback(session)
        with self.assertRaises(ConnectorError) as raised:
            await w.prepare_archive(TARGET)
        self.assertEqual(raised.exception.code, 'TARGET_NOT_SUPERSEDED')

    async def test_protected_scopes_are_refused(self):
        for path in ('core/核心记忆', 'wiki/profile/lingxi-connector-abc'):
            session = ArchiveSession(path=path, content=record())
            w = self.writeback(session)
            with self.assertRaises(ConnectorError) as raised:
                await w.prepare_archive(path)
            self.assertEqual(raised.exception.code, 'PROTECTED_TARGET')
            self.assertEqual(session.deletes, 0)

    async def test_replace_cannot_target_protected_scopes(self):
        for path in ('core/核心记忆', 'wiki/profile/lingxi-connector-abc'):
            session = ArchiveSession(path=path, content=record())
            w = self.writeback(session)
            with self.assertRaises(ConnectorError) as raised:
                await w.prepare('concepts', 't', 's', 'body', target_path=path)
            self.assertEqual(raised.exception.code, 'PROTECTED_TARGET')

    async def test_commit_refuses_when_page_changed_after_preview(self):
        session = ArchiveSession(content=record())
        w = self.writeback(session)
        p = await w.prepare_archive(TARGET)
        session.pages[TARGET]['content'] = 'changed after preview'
        r = await w.commit(p['proposal_id'])
        self.assertEqual(r['state'], 'rejected')
        self.assertEqual(r['error'], 'TARGET_CHANGED')
        self.assertEqual(session.deletes, 0)

    async def test_commit_refuses_when_page_already_gone(self):
        session = ArchiveSession(content=record())
        w = self.writeback(session)
        p = await w.prepare_archive(TARGET)
        del session.pages[TARGET]
        r = await w.commit(p['proposal_id'])
        self.assertEqual(r['state'], 'rejected')
        self.assertEqual(r['error'], 'TARGET_MISSING')
        self.assertEqual(session.deletes, 0)

    async def test_second_commit_returns_the_same_receipt(self):
        session = ArchiveSession(content=record())
        w = self.writeback(session)
        p = await w.prepare_archive(TARGET)
        first = await w.commit(p['proposal_id'])
        second = await w.commit(p['proposal_id'])
        self.assertEqual(first, second)
        self.assertEqual(session.deletes, 1)

    async def test_partial_read_is_refused(self):
        session = ArchiveSession(content=record(), truncated=[TARGET])
        w = self.writeback(session)
        with self.assertRaises(ConnectorError) as raised:
            await w.prepare_archive(TARGET)
        self.assertEqual(raised.exception.code, 'TARGET_READ_INCOMPLETE')
        self.assertEqual(session.deletes, 0)


if __name__ == '__main__':
    unittest.main()
