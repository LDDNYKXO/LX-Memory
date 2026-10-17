import asyncio
import io
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError, URLError
from lingxi_connector.broker import Broker
from lingxi_connector.broker_client import _transport_timeout, call
from lingxi_connector.cloud import ConnectorError

class BrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.broker = Broker(Path(self.temp.name))
        self.task = asyncio.create_task(self.broker.run())
        for _ in range(100):
            if self.broker.port:
                break
            await asyncio.sleep(.01)

    async def asyncTearDown(self):
        self.broker.stop.set()
        await self.task
        self.temp.cleanup()

    async def request(self, operation, body=None, token=None, origin=None):
        def send():
            headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + (token or self.broker.token)}
            if origin:
                headers['Origin'] = origin
            req = Request('http://127.0.0.1:%d/v1/%s' % (self.broker.port, operation), data=json.dumps(body or {}).encode(), headers=headers)
            try:
                with build_opener(ProxyHandler({})).open(req, timeout=5) as r:
                    return r.status, json.load(r)
            except HTTPError as e:
                return e.code, json.load(e)
        return await asyncio.to_thread(send)

    async def test_reject_wrong_token_and_browser_origin(self):
        self.assertEqual((await self.request('status', token='wrong'))[0], 403)
        self.assertEqual((await self.request('status', origin='https://evil.test'))[0], 403)

    async def test_empty_session_two_concurrent_clients(self):
        a, b = await asyncio.gather(self.request('status'), self.request('search', {'query':'测试'}))
        self.assertEqual(a[1]['data']['state'], 'NOT_LOGGED_IN')
        self.assertEqual(b[1]['error']['code'], 'AUTH_REQUIRED')
        self.assertIsNone(self.broker.session.context)

    async def test_logout_rotates_token(self):
        token = self.broker.token
        self.assertTrue((await self.request('logout'))[1]['ok'])
        self.assertEqual((await self.request('status', token=token))[0], 403)
        self.assertEqual((await self.request('status'))[0], 200)


class FakeSession:
    """Read-only session double: records every call and exposes `epoch` like a real login/logout."""

    def __init__(self):
        self.epoch = 0
        self.calls = []
        self.remember = True
        self.logged_in = True

    async def status(self, refresh=False):
        self.calls.append('status')
        if not self.logged_in:
            return {'state': 'NOT_LOGGED_IN', 'cloud_available': False, 'error': None}
        return {'state': 'CONNECTED', 'cloud_available': True, 'error': None}

    async def search(self, query, limit=5):
        self.calls.append('search:' + str(query))
        return {'total': 1, 'results': [{'path': 'wiki/projects/a/index', 'title': '灵犀记忆',
                                         'summary': None, 'snippet': None, 'score': 1.0,
                                         'updated': '2026-10-01T00:00:00+00:00',
                                         'front_matter': {'project': '灵犀记忆'}}]}

    async def read(self, path, offset=0, max_chars=8000):
        self.calls.append('read:' + str(path))
        return {'path': path, 'front_matter': {'next_step': '继续 Task 6'}, 'content': '正文',
                'updated': '2026-10-01T00:00:00+00:00', 'retrieved_at': '2026-10-15T22:00:00+00:00',
                'source': 'lingxi_cloud', 'content_total_chars': 2, 'offset': 0, 'truncated': False,
                'next_offset': None, 'content_scope': 'full'}

    async def logout(self):
        self.epoch += 1
        self.logged_in = False
        return {'state': 'NOT_LOGGED_IN'}

    async def close(self):
        return None


class ContextOperationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.broker = Broker(Path(self.temp.name))
        self.task = asyncio.create_task(self.broker.run())
        for _ in range(100):
            if self.broker.port:
                break
            await asyncio.sleep(.01)

    async def asyncTearDown(self):
        self.broker.stop.set()
        await self.task
        self.temp.cleanup()

    async def request(self, operation, body=None, token=None):
        def send():
            headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + (token or self.broker.token)}
            req = Request('http://127.0.0.1:%d/v1/%s' % (self.broker.port, operation), data=json.dumps(body or {}).encode(), headers=headers)
            try:
                with build_opener(ProxyHandler({})).open(req, timeout=5) as r:
                    return r.status, json.load(r)
            except HTTPError as e:
                return e.code, json.load(e)
        return await asyncio.to_thread(send)

    def use_fake_session(self):
        self.fake = FakeSession()
        self.broker.session = self.fake
        return self.fake

    def args(self, **overrides):
        body = {'request': '继续这个项目', 'project': '灵犀记忆', 'request_id': 'r1'}
        body.update(overrides)
        return body

    async def test_context_returns_service_unavailable_when_not_logged_in(self):
        code, body = await self.request('context', self.args())
        self.assertEqual(code, 200)
        self.assertEqual(body['data']['state'], 'service_unavailable')
        self.assertEqual(body['data']['error']['code'], 'AUTH_REQUIRED')

    async def test_context_rejects_unknown_arguments(self):
        code, body = await self.request('context', {'request': 'x', 'evil': 1})
        self.assertEqual(body['error']['code'], 'INVALID_INPUT')

    async def test_same_round_request_is_fetched_once(self):
        fake = self.use_fake_session()
        first = await self.request('context', self.args())
        second = await self.request('context', self.args())
        self.assertEqual([call for call in fake.calls if call.startswith('search')],
                         ['search:灵犀记忆', 'search:用户画像 协作偏好 经验 教训'])
        self.assertEqual(first[1]['data']['request_id'], second[1]['data']['request_id'])
        self.assertNotIn('cached', first[1]['data'])
        self.assertTrue(second[1]['data']['cached'])

    async def test_concurrent_identical_requests_are_coalesced(self):
        fake = self.use_fake_session()
        results = await asyncio.gather(self.request('context', self.args()), self.request('context', self.args()))
        self.assertEqual([call for call in fake.calls if call.startswith('search')],
                         ['search:灵犀记忆', 'search:用户画像 协作偏好 经验 教训'])
        self.assertEqual([item[1]['data']['state'] for item in results], ['partial', 'partial'])

    async def test_different_request_ids_are_not_merged(self):
        fake = self.use_fake_session()
        await self.request('context', self.args(request_id='r1'))
        await self.request('context', self.args(request_id='r2'))
        self.assertEqual(len([call for call in fake.calls if call.startswith('search')]), 4)

    async def test_a_different_project_clue_is_never_answered_from_the_same_round_cache(self):
        # Live-cloud regression: same request text, different project name — the second call was
        # served from the first project's cache and returned the wrong project's background.
        fake = self.use_fake_session()
        first = await self.request('context', self.args(project='灵犀记忆'))
        second = await self.request('context', self.args(project='另一个项目'))
        searches = [call for call in fake.calls if call.startswith('search')]
        self.assertEqual(searches, ['search:灵犀记忆', 'search:用户画像 协作偏好 经验 教训',
                                    'search:另一个', 'search:另一个项目'])
        self.assertNotIn('cached', second[1]['data'])

    async def test_cwd_and_remote_also_take_part_in_the_cache_key(self):
        fake = self.use_fake_session()
        await self.request('context', self.args(cwd='D:\\a', remote='github.com/o/r'))
        await self.request('context', self.args(cwd='D:\\b', remote='github.com/o/r'))
        await self.request('context', self.args(cwd='D:\\b', remote='github.com/o/other'))
        self.assertEqual(len([call for call in fake.calls if call.startswith('search')]), 6)

    async def test_an_unexpected_failure_is_not_left_in_the_cache(self):
        self.use_fake_session()
        attempts = []

        async def boom(*args, **kwargs):
            attempts.append(1)
            raise RuntimeError('unexpected')

        body = self.args()
        with patch('lingxi_connector.broker.build_context', boom):
            with self.assertRaises(RuntimeError):
                await self.broker.context(body)
            with self.assertRaises(RuntimeError):
                await self.broker.context(body)
        self.assertEqual(len(attempts), 2)

    async def test_cache_is_invalidated_by_epoch(self):
        fake = self.use_fake_session()
        await self.request('context', self.args())
        await self.broker.session.logout()
        self.broker.session.logged_in = True
        await self.request('context', self.args())
        self.assertEqual(len([call for call in fake.calls if call.startswith('search')]), 4)

    async def test_context_accepts_the_repository_identity_argument(self):
        self.use_fake_session()
        code, body = await self.request('context', {'request': '继续这个项目',
                                                    'remote': 'github.com/o/r'})
        self.assertEqual(code, 200)
        self.assertIn(body['data']['state'], ('complete', 'partial'))

    async def test_context_only_ever_calls_read_only_operations(self):
        fake = self.use_fake_session()
        await self.request('context', self.args())
        self.assertTrue(fake.calls)
        for call in fake.calls:
            self.assertTrue(call == 'status' or call.startswith('search:') or call.startswith('read:'), call)


class TransportTimeoutTests(unittest.TestCase):
    def test_default_timeouts_unchanged_for_existing_operations(self):
        self.assertEqual(_transport_timeout('status'), 65)
        self.assertEqual(_transport_timeout('search'), 65)
        self.assertEqual(_transport_timeout('read'), 65)
        self.assertEqual(_transport_timeout('enable-saving'), 240)

    def test_context_uses_the_total_budget(self):
        self.assertEqual(_transport_timeout('context'), 45)

    def test_explicit_timeout_wins(self):
        self.assertEqual(_transport_timeout('context', 5), 5)
        self.assertEqual(_transport_timeout('search', 5), 5)

    def test_call_hands_the_context_budget_to_the_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / 'runtime/session'
            session.mkdir(parents=True)
            (session / 'connection.json').write_text(json.dumps({'port': 4444, 'token': 'x' * 32, 'pid': 1, 'version': 1}))
            opener = Mock()
            payload = json.dumps({'ok': True, 'data': {'state': 'complete'}}).encode()
            opener.open.return_value.__enter__ = Mock(return_value=io.BytesIO(payload))
            opener.open.return_value.__exit__ = Mock(return_value=False)
            with patch('lingxi_connector.broker_client.build_opener', return_value=opener):
                result = call(root, 'context', {'request': '继续'})
        self.assertEqual(result, {'state': 'complete'})
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 45)

    def test_call_keeps_the_old_default_for_existing_operations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / 'runtime/session'
            session.mkdir(parents=True)
            (session / 'connection.json').write_text(json.dumps({'port': 4444, 'token': 'x' * 32, 'pid': 1, 'version': 1}))
            opener = Mock()
            payload = json.dumps({'ok': True, 'data': {'total': 0, 'results': []}}).encode()
            opener.open.return_value.__enter__ = Mock(return_value=io.BytesIO(payload))
            opener.open.return_value.__exit__ = Mock(return_value=False)
            with patch('lingxi_connector.broker_client.build_opener', return_value=opener):
                call(root, 'search', {'query': '测试'})
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 65)

    def test_a_context_timeout_is_not_retried_fifty_times(self):
        # The transport budget equals the server budget, so re-issuing the same request would repeat
        # the whole cost and could block the caller for ~37 minutes before a misleading error.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / 'runtime/session'
            session.mkdir(parents=True)
            (session / 'connection.json').write_text(json.dumps({'port': 4444, 'token': 'x' * 32, 'pid': 1, 'version': 1}))
            opener = Mock()
            opener.open.side_effect = socket.timeout('timed out')
            with patch('lingxi_connector.broker_client.build_opener', return_value=opener):
                with self.assertRaises(ConnectorError) as raised:
                    call(root, 'context', {'request': '继续'})
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(raised.exception.code, 'NETWORK_ERROR')

    def test_a_wrapped_context_timeout_is_not_retried_either(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / 'runtime/session'
            session.mkdir(parents=True)
            (session / 'connection.json').write_text(json.dumps({'port': 4444, 'token': 'x' * 32, 'pid': 1, 'version': 1}))
            opener = Mock()
            opener.open.side_effect = URLError(socket.timeout('timed out'))
            with patch('lingxi_connector.broker_client.build_opener', return_value=opener):
                with self.assertRaises(ConnectorError) as raised:
                    call(root, 'context', {'request': '继续'})
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(raised.exception.code, 'NETWORK_ERROR')


if __name__ == '__main__':
    unittest.main()
