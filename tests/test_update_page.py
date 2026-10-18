"""Update tests: whole-page replacement via prepare(target_path=...) + commit."""
import hashlib
import tempfile
import unittest
from pathlib import Path

from lingxi_connector.cloud import ConnectorError
from lingxi_connector.writeback import Writeback


class ReplaceSession:
    """Minimal session: one existing page, records whether POST or PUT was used."""

    epoch = 0
    account = 'account-a'
    create_contract_verified = True

    def __init__(self, content='old body', title='old title'):
        self.posts = 0
        self.puts = 0
        self.deleted = 0
        self.page = {'path': 'wiki/concepts/lingxi-connector-existing',
                     'front_matter': {'title': title, 'summary': 's', 'sources': [], 'related': []},
                     'content': content, 'source': 'lingxi_cloud', 'content_scope': 'full',
                     'truncated': False, 'updated': 1, 'retrieved_at': '2026-10-17T00:00:00+00:00'}

    def write_enabled_for(self, account):
        return self.create_contract_verified

    async def identity(self):
        return self.account

    async def status(self, refresh=False):
        return {'state': 'CONNECTED', 'cloud_available': True}

    async def search(self, query, limit):
        return {'source': 'lingxi_cloud', 'total': 0, 'results': []}

    async def read(self, path, offset=0, max_chars=20000):
        if path != self.page['path']:
            raise ConnectorError('PAGE_NOT_FOUND')
        return dict(self.page)

    async def create_page(self, payload, expected_account=None, expected_epoch=None):
        self.posts += 1
        self.page = {**payload, 'source': 'lingxi_cloud', 'content_scope': 'full',
                     'truncated': False, 'updated': 2,
                     'retrieved_at': '2026-10-17T00:01:00+00:00'}
        return {}

    async def replace_page(self, payload, expected_account=None, expected_epoch=None):
        self.puts += 1
        if payload['path'] != self.page['path']:
            raise ConnectorError('INVALID_INPUT')
        self.page = {**payload, 'source': 'lingxi_cloud', 'content_scope': 'full',
                     'truncated': False, 'updated': 3,
                     'retrieved_at': '2026-10-17T00:02:00+00:00'}
        return {}


class ReplaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = ReplaceSession()
        self.w = Writeback(self.session, Path(self.tmp.name))

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_preview_with_target_path_shows_previous_body_and_writes_nothing(self):
        p = await self.w.prepare('concepts', 'new title', 'new summary', 'new body',
                                 target_path='wiki/concepts/lingxi-connector-existing')
        self.assertEqual(p['operation'], 'replace')
        self.assertEqual(p['preview']['path'], 'wiki/concepts/lingxi-connector-existing')
        self.assertEqual(p['preview']['content'], 'new body')
        self.assertEqual(p['replacing']['previous_title'], 'old title')
        self.assertEqual(p['replacing']['previous_content'], 'old body')
        self.assertEqual((self.session.posts, self.session.puts), (0, 0))

    async def test_commit_uses_put_and_verifies_readback(self):
        p = await self.w.prepare('concepts', 'new title', 'new summary', 'new body',
                                 target_path='wiki/concepts/lingxi-connector-existing')
        r = await self.w.commit(p['proposal_id'])
        self.assertEqual(r['state'], 'saved_verified')
        self.assertEqual((self.session.posts, self.session.puts), (0, 1))
        self.assertEqual(self.session.page['content'], 'new body')

    async def test_commit_refuses_when_page_changed_after_preview(self):
        p = await self.w.prepare('concepts', 'new title', 'new summary', 'new body',
                                 target_path='wiki/concepts/lingxi-connector-existing')
        self.session.page['content'] = 'someone else edited this'
        r = await self.w.commit(p['proposal_id'])
        self.assertEqual(r['state'], 'rejected')
        self.assertEqual(r['error'], 'TARGET_CHANGED')
        self.assertEqual(self.session.puts, 0)

    async def test_commit_refuses_when_page_disappeared_after_preview(self):
        p = await self.w.prepare('concepts', 'new title', 'new summary', 'new body',
                                 target_path='wiki/concepts/lingxi-connector-existing')
        original = self.session.page

        async def missing(path, offset=0, max_chars=20000):
            raise ConnectorError('PAGE_NOT_FOUND')

        self.session.read = missing
        r = await self.w.commit(p['proposal_id'])
        self.assertEqual(r['state'], 'rejected')
        self.assertEqual(r['error'], 'TARGET_MISSING')
        self.assertEqual(self.session.puts, 0)
        self.session.page = original

    async def test_preview_rejects_partial_target_read(self):
        page = dict(self.session.page)
        page['content_scope'] = 'partial'
        page['truncated'] = True

        async def partial(path, offset=0, max_chars=20000):
            return page

        self.session.read = partial
        with self.assertRaises(ConnectorError) as raised:
            await self.w.prepare('concepts', 't', 's', 'c',
                                 target_path='wiki/concepts/lingxi-connector-existing')
        self.assertEqual(raised.exception.code, 'TARGET_READ_INCOMPLETE')

    async def test_preview_rejects_non_wiki_target(self):
        with self.assertRaises(ConnectorError):
            await self.w.prepare('concepts', 't', 's', 'c', target_path='core/核心记忆')

    async def test_create_path_is_unchanged_without_target_path(self):
        p = await self.w.prepare('concepts', 't', 's', 'body')
        self.assertEqual(p['operation'], 'create')
        self.assertTrue(p['preview']['path'].startswith('wiki/concepts/lingxi-connector-'))
        r = await self.w.commit(p['proposal_id'])
        self.assertEqual(r['state'], 'saved_verified')
        self.assertEqual((self.session.posts, self.session.puts), (1, 0))
