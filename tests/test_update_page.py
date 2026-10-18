"""Update tests: whole-page replacement, metadata binding, and final-read ordering."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lingxi_connector.cloud import ConnectorError
from lingxi_connector import page_meta
from lingxi_connector.writeback import Writeback

TARGET = 'wiki/concepts/lingxi-connector-existing'


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class ReplaceSession:
    """Minimal session: one existing page, records read/write order and payloads."""

    epoch = 0
    account = 'account-a'
    create_contract_verified = True

    def __init__(self, content='old body', title='old title',
                 front_matter=None):
        self.posts = 0
        self.puts = 0
        self.deleted = 0
        self.events = []
        self.missing = False
        self.partial_after = None
        self.put_error = None
        self.page = {'path': TARGET,
                     'front_matter': front_matter or {
                         'title': title, 'summary': 'old summary',
                         'sources': ['wiki/projects/source-1'],
                         'related': ['wiki/lessons/related-1']},
                     'content': content, 'source': 'lingxi_cloud', 'content_scope': 'full',
                     'truncated': False, 'updated': 1,
                     'retrieved_at': '2026-10-17T00:00:00+00:00'}

    def write_enabled_for(self, account):
        return self.create_contract_verified

    async def identity(self):
        return self.account

    async def status(self, refresh=False):
        return {'state': 'CONNECTED', 'cloud_available': True}

    async def search(self, query, limit=5):
        return {'source': 'lingxi_cloud', 'total': 0, 'results': []}

    async def read(self, path, offset=0, max_chars=20000):
        self.events.append('read:' + path)
        if self.missing or path != self.page['path']:
            raise ConnectorError('PAGE_NOT_FOUND')
        if self.partial_after is not None and len([e for e in self.events if e.startswith('read:')]) > self.partial_after:
            return dict(self.page, content_scope='partial', truncated=True)
        return dict(self.page)

    async def create_page(self, payload, expected_account=None, expected_epoch=None):
        self.posts += 1
        self.events.append('put:create')
        self.page = {**payload, 'source': 'lingxi_cloud', 'content_scope': 'full',
                     'truncated': False, 'updated': 2,
                     'retrieved_at': '2026-10-17T00:01:00+00:00'}
        return {}

    async def replace_page(self, payload, expected_account=None, expected_epoch=None):
        self.puts += 1
        self.events.append('put:replace')
        if self.put_error is not None:
            raise self.put_error
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
        self.clock = Clock()
        self.w = Writeback(self.session, Path(self.tmp.name), clock=self.clock)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_preview_shows_previous_body_and_metadata(self):
        p = await self.w.prepare('concepts', 'new title', 'new summary', 'new body', target_path=TARGET)
        self.assertEqual(p['operation'], 'replace')
        self.assertEqual(p['preview']['content'], 'new body')
        self.assertEqual(p['replacing']['previous_content'], 'old body')
        self.assertEqual(p['replacing']['previous_front_matter']['sources'], ['wiki/projects/source-1'])
        self.assertEqual(p['replacing']['next_front_matter']['sources'], ['wiki/projects/source-1'])
        self.assertEqual(p['preview']['front_matter']['sources'], ['wiki/projects/source-1'])
        self.assertEqual((self.session.posts, self.session.puts), (0, 0))

    async def test_explicit_metadata_replacement_is_visible_in_preview(self):
        p = await self.w.prepare('concepts', 'new title', 'new summary', 'new body', target_path=TARGET,
                                 sources=[], related=['wiki/lessons/new-related'])
        self.assertEqual(p['replacing']['next_front_matter']['sources'], [])
        self.assertEqual(p['replacing']['next_front_matter']['related'], ['wiki/lessons/new-related'])
        r = await self.w.commit(p['proposal_id'])
        self.assertEqual(r['state'], 'saved_verified')
        self.assertEqual(self.session.page['front_matter']['sources'], [])
        self.assertEqual(self.session.page['front_matter']['related'], ['wiki/lessons/new-related'])

    async def test_commit_uses_put_and_verifies_readback(self):
        p = await self.w.prepare('concepts', 'new title', 'new summary', 'new body', target_path=TARGET)
        r = await self.w.commit(p['proposal_id'])
        self.assertEqual(r['state'], 'saved_verified')
        self.assertEqual((self.session.posts, self.session.puts), (0, 1))
        self.assertEqual(self.session.page['content'], 'new body')
        self.assertEqual(self.session.page['front_matter']['sources'], ['wiki/projects/source-1'])

    async def test_commit_refuses_metadata_only_change(self):
        for field, value in (('title', 'other'), ('summary', 'other'),
                             ('sources', ['wiki/projects/other']), ('related', [])):
            self.session = ReplaceSession()
            self.w = Writeback(self.session, Path(self.tmp.name), clock=self.clock)
            p = await self.w.prepare('concepts', 'new title', 'new summary', 'new body', target_path=TARGET)
            self.session.page['front_matter'][field] = value
            r = await self.w.commit(p['proposal_id'])
            self.assertEqual((r['state'], r['error']), ('rejected', 'TARGET_METADATA_CHANGED'), field)
            self.assertEqual(self.session.puts, 0)

    async def test_commit_refuses_content_change(self):
        p = await self.w.prepare('concepts', 'new title', 'new summary', 'new body', target_path=TARGET)
        self.session.page['content'] = 'someone else edited this'
        r = await self.w.commit(p['proposal_id'])
        self.assertEqual((r['state'], r['error']), ('rejected', 'TARGET_CHANGED'))
        self.assertEqual(self.session.puts, 0)

    async def test_commit_refuses_when_page_disappeared_after_preview(self):
        p = await self.w.prepare('concepts', 'new title', 'new summary', 'new body', target_path=TARGET)
        self.session.missing = True
        r = await self.w.commit(p['proposal_id'])
        self.assertEqual((r['state'], r['error']), ('rejected', 'TARGET_MISSING'))
        self.assertEqual(self.session.puts, 0)

    async def test_commit_refuses_when_final_read_is_partial(self):
        p = await self.w.prepare('concepts', 'new title', 'new summary', 'new body', target_path=TARGET)
        self.session.partial_after = len([e for e in self.session.events if e.startswith('read:')]) + 1
        r = await self.w.commit(p['proposal_id'])
        self.assertEqual((r['state'], r['error']), ('rejected', 'TARGET_UNREADABLE'))
        self.assertEqual(self.session.puts, 0)

    async def test_preview_rejects_partial_target_read(self):
        page = dict(self.session.page)
        page['content_scope'] = 'partial'
        page['truncated'] = True

        async def partial(path, offset=0, max_chars=20000):
            return page

        self.session.read = partial
        with self.assertRaises(ConnectorError) as raised:
            await self.w.prepare('concepts', 't', 's', 'c', target_path=TARGET)
        self.assertEqual(raised.exception.code, 'TARGET_READ_INCOMPLETE')

    async def test_preview_rejects_non_wiki_target(self):
        with self.assertRaises(ConnectorError):
            await self.w.prepare('concepts', 't', 's', 'c', target_path='core/核心记忆')

    async def test_validation_runs_before_the_final_read(self):
        project_target = 'wiki/projects/lingxi-connector-existing'
        self.session.page['path'] = project_target
        with patch('lingxi_connector.writeback.validate_proposal') as validation:
            async def passes(*args, **kwargs):
                self.session.events.append('validate')
                return {'state': 'ok'}
            validation.side_effect = passes
            p = await self.w.prepare('projects', 't', 's', 'body', target_path=project_target)
            if p.get('blocked_reason') == 'ASSESSMENT_REVIEW_REQUIRED':
                p = await self.w.prepare('projects', 't', 's', 'body', target_path=project_target,
                                         assessment_confirmation=p['assessment_review_key'])
            r = await self.w.commit(p['proposal_id'])
        self.assertEqual(r['state'], 'saved_verified')
        events = self.session.events
        put_index = events.index('put:replace')
        self.assertIn('validate', events)
        self.assertLess(events.index('validate'), put_index)
        self.assertTrue(events[put_index - 1].startswith('read:' + project_target), events)
        self.assertNotIn('validate', events[put_index:])

    async def test_put_conflict_is_not_retried(self):
        p = await self.w.prepare('concepts', 'new title', 'new summary', 'new body', target_path=TARGET)
        self.session.put_error = ConnectorError('UPSTREAM_INCOMPATIBLE')
        r = await self.w.commit(p['proposal_id'])
        self.assertEqual(r['state'], 'unknown')
        self.assertEqual(r['error'], 'UPSTREAM_INCOMPATIBLE')
        self.assertEqual(self.session.puts, 1)

    async def test_remote_race_between_final_read_and_put_is_a_known_residual(self):
        p = await self.w.prepare('concepts', 'new title', 'new summary', 'new body', target_path=TARGET)
        original = self.session.replace_page

        async def racing_replace(payload, expected_account=None, expected_epoch=None):
            # No upstream conditional-write contract exists, so a writer between the final
            # read and PUT is not detectable here; the write proceeds without retry.
            self.session.page['content'] = 'written by another client'
            return await original(payload, expected_account, expected_epoch)

        self.session.replace_page = racing_replace
        r = await self.w.commit(p['proposal_id'])
        self.assertEqual(r['state'], 'saved_verified')
        self.assertEqual(self.session.puts, 1)

    async def test_create_path_is_unchanged_without_target_path(self):
        p = await self.w.prepare('concepts', 't', 's', 'body')
        self.assertEqual(p['operation'], 'create')
        self.assertEqual(p['preview']['front_matter'],
                         {'title': 't', 'summary': 's', 'sources': [], 'related': []})
        r = await self.w.commit(p['proposal_id'])
        self.assertEqual(r['state'], 'saved_verified')
        self.assertEqual((self.session.posts, self.session.puts), (1, 0))


    async def test_create_with_tags_and_attrs(self):
        p = await self.w.prepare('concepts', 't', 's', 'body', tags=['偏好', '决策'], attrs={'领域': '记忆'})
        self.assertEqual(p['tags'], ['偏好', '决策'])
        self.assertEqual(p['attrs'], {'领域': '记忆'})
        self.assertIn('<!-- lx-memory-meta', p['preview']['content'])
        r = await self.w.commit(p['proposal_id'])
        self.assertEqual(r['state'], 'saved_verified')
        self.assertIn('<!-- lx-memory-meta', self.session.page['content'])

    async def test_replace_preserves_tags_by_default(self):
        self.session.page['content'] = page_meta.with_meta('old body', ['偏好'], {'领域': '记忆'})
        p = await self.w.prepare('concepts', 'new title', 'new summary', 'new body', target_path=TARGET)
        self.assertEqual(p['tags'], ['偏好'])
        self.assertEqual(p['attrs'], {'领域': '记忆'})
        self.assertEqual(p['replacing']['previous_tags'], ['偏好'])
        r = await self.w.commit(p['proposal_id'])
        self.assertEqual(r['state'], 'saved_verified')
        body, tags, attrs = page_meta.split(self.session.page['content'])
        self.assertEqual(body, 'new body')
        self.assertEqual((tags, attrs), (['偏好'], {'领域': '记忆'}))

    async def test_replace_can_clear_or_change_tags(self):
        self.session.page['content'] = page_meta.with_meta('old body', ['偏好'], {'领域': '记忆'})
        p = await self.w.prepare('concepts', 'new title', 'new summary', 'new body',
                                 target_path=TARGET, tags=[], attrs={})
        self.assertEqual(p['tags'], [])
        self.assertEqual(p['attrs'], {})
        r = await self.w.commit(p['proposal_id'])
        self.assertEqual(r['state'], 'saved_verified')
        self.assertNotIn('<!-- lx-memory-meta', self.session.page['content'])
        self.assertEqual(self.session.page['content'], 'new body')

    async def test_invalid_tags_are_rejected(self):
        with self.assertRaises(ConnectorError) as raised:
            await self.w.prepare('concepts', 't', 's', 'body', tags=['x'] * 11)
        self.assertEqual(raised.exception.code, 'INVALID_INPUT')


if __name__ == '__main__':
    unittest.main()
