import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError
from lingxi_connector.broker import Broker

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
