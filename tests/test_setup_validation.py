import json
from pathlib import Path
import tempfile
import unittest
from lingxi_connector.session import Session
from lingxi_connector.cloud import IDENTITY_URL, PREFIX, ConnectorError

class Response:
    def __init__(self,url,status,body):self.url=url;self.status=status;self.body=body
    async def text(self):return json.dumps(self.body)
    async def dispose(self):pass
class Request:
    def __init__(self):self.posts=[];self.pages={};self.user='123';self.fail_read=False
    async def get(self,url,**kwargs):
        if url==IDENTITY_URL:return Response(url,200,{'result':'ok','userid':self.user})
        page=self.pages.get(url)
        if page is None:return Response(url,404,{})
        if self.fail_read:raise TimeoutError('cloud GET unavailable after POST')
        return Response(url,200,{'result':'ok','v7code':0,'data':{**page,'updated':1}})
    async def post(self,url,data,**kwargs):
        self.posts.append((url,data))
        page_url=PREFIX+'pages/'+data['path']
        if page_url not in self.pages:
            self.pages[page_url]=data
            return Response(url,200,{'result':'ok','v7code':0,'data':{}})
        return Response(url,409,{'result':'ResourceConflict','v7code':409433001})
class ValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_temporary_login_validation_same_content_two_posts(self):
        with tempfile.TemporaryDirectory() as d:
            s=Session(Path(d));request=Request()
            class Context:pass
            s.context=Context();s.context.request=request
            self.assertFalse(s.remember)
            result=await s.verify_create_contract()
            self.assertTrue(result['contract_verified'])
            self.assertTrue(s.write_enabled_for('123'))
            self.assertFalse(s.write_enabled_for('456'))
            self.assertEqual(len(request.posts),2)
            self.assertEqual(request.posts[0],request.posts[1])
            self.assertEqual(len(list(Path(d).glob('create-contract-*.json'))),1)
    async def test_no_login_does_not_start_validation_or_write(self):
        with tempfile.TemporaryDirectory() as d:
            s=Session(Path(d))
            with self.assertRaises(ConnectorError) as e:await s.verify_create_contract()
            self.assertEqual(e.exception.code,'AUTH_REQUIRED')
            self.assertIsNone(s.context)
    async def test_unknown_probe_is_not_repeated_on_new_call(self):
        with tempfile.TemporaryDirectory() as d:
            s=Session(Path(d));request=Request();request.fail_read=True
            class Context:pass
            s.context=Context();s.context.request=request
            first=await s.verify_create_contract()
            second=await s.verify_create_contract()
            self.assertFalse(first['contract_verified'])
            self.assertEqual(first['path'],second['path'])
            self.assertEqual(len(request.posts),1)
    async def test_account_switch_retains_each_unknown_transaction(self):
        with tempfile.TemporaryDirectory() as d:
            s=Session(Path(d));request=Request();request.fail_read=True
            class Context:pass
            s.context=Context();s.context.request=request
            first=await s.verify_create_contract()
            request.user='456';await s.verify_create_contract()
            request.user='123';again=await s.verify_create_contract()
            self.assertEqual(first['path'],again['path'])
            self.assertEqual(len(request.posts),2)
    async def test_verified_accounts_reuse_proofs_after_switch(self):
        with tempfile.TemporaryDirectory() as d:
            s=Session(Path(d));request=Request()
            class Context:pass
            s.context=Context();s.context.request=request
            self.assertTrue((await s.verify_create_contract())['contract_verified'])
            request.user='456';self.assertTrue((await s.verify_create_contract())['contract_verified'])
            request.user='123';self.assertTrue((await s.verify_create_contract())['reused'])
            self.assertEqual(len(request.posts),4)
