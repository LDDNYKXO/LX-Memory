import unittest
import tempfile
from pathlib import Path
from lingxi_connector.session import Session
from lingxi_connector.cloud import ConnectorError, identity_result
from unittest.mock import AsyncMock
import json
import hashlib

class TransportTests(unittest.IsolatedAsyncioTestCase):
    def test_identity_rejects_unknown_or_not_logged_in(self):
        self.assertEqual(identity_result(200, {'result':'ok','userid':123}), '123')
        for body in ({'result':'userNotLogin'}, {'result':'ok'}, {'result':'ok','userid':True}):
            with self.assertRaises(ConnectorError):
                identity_result(200, body)

    async def test_creation_is_disabled_without_contract_proof(self):
        with tempfile.TemporaryDirectory() as d:
            s = Session(Path(d))
            with self.assertRaises(ConnectorError) as e:
                await s.create_page({'path':'wiki/concepts/lingxi-connector-test','front_matter':{},'content':'test'})
            self.assertEqual(e.exception.code, 'UPSTREAM_CREATE_UNPROVEN')
            self.assertIsNone(s.context)
    async def test_logout_during_identity_dispose_prevents_post(self):
        with tempfile.TemporaryDirectory() as d:
            s=Session(Path(d));s.create_contract_verified=True
            s.create_contract_account_tag=hashlib.sha256(b'123').hexdigest()
            class Response:
                status=200
                url='https://account.kdocs.cn/api/v3/islogin'
                async def text(self):return json.dumps({'result':'ok','userid':123})
                async def dispose(self):s.epoch+=1
            class Request:
                get=AsyncMock(return_value=Response())
                post=AsyncMock(side_effect=RuntimeError('unexpected POST'))
            class Context:request=Request()
            s.context=Context()
            with self.assertRaises(ConnectorError):
                await s.create_page({'path':'wiki/concepts/lingxi-connector-test','front_matter':{},'content':'test'},'123',0)
            s.context.request.post.assert_not_awaited()
    def test_verified_contract_is_private_and_account_bound(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d)
            proof={'contract_verified':True,'identity_checked':True,'first_readback_match':True,'duplicate_rejected':True,'second_readback_match':True,'origin':'https://lingxi.kdocs.cn/api/public/v1/agents/default/memory/','account_tag':hashlib.sha256(b'123').hexdigest(),'duplicate_response':{'http_status':409,'result':'ResourceConflict'}}
            (base/'create-contract.json').write_text(json.dumps(proof))
            s=Session(base)
            self.assertTrue(s.write_enabled_for('123'))
            self.assertFalse(s.write_enabled_for('456'))
