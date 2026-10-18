import asyncio
import tempfile
import unittest
from pathlib import Path
from lingxi_connector.cloud import ConnectorError
from lingxi_connector.writeback import Writeback

class FakeSession:
    epoch = 0
    account = 'account-a'
    create_contract_verified = True
    posts = 0
    fail_post = False
    mismatch = False
    page = None
    def write_enabled_for(self,account):return self.create_contract_verified
    async def identity(self): return self.account
    async def status(self, refresh=False): return {'state': 'CONNECTED', 'cloud_available': True}
    async def search(self, query, limit): return {'results':[]}
    async def read(self, path, offset=0, max_chars=20000):
        if self.page is None: raise ConnectorError('PAGE_NOT_FOUND')
        return {**self.page,'content':'wrong' if self.mismatch else self.page['content'], 'content_scope':'full','truncated':False,'source':'lingxi_cloud','updated':1}
    async def create_page(self, payload, expected_account, expected_epoch):
        self.posts += 1
        self.page = payload
        if self.fail_post: raise ConnectorError('WRITE_OUTCOME_UNKNOWN')
        return {}

class WritebackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = FakeSession()
        self.clock = [0]
        self.w = Writeback(self.session, Path(self.tmp.name), clock=lambda:self.clock[0])
    async def asyncTearDown(self): self.tmp.cleanup()
    async def prepare(self): return await self.w.prepare('concepts','标题','摘要','正文')
    async def test_preview_does_not_write(self):
        p = await self.prepare()
        self.assertEqual(p['preview']['content'],'正文')
        self.assertEqual(self.session.posts,0)
        self.assertNotIn('account',p)
    async def test_invalid_input_no_write(self):
        for category,title,summary,content in [('core','x','','x'),('concepts','x','',''),('concepts','x','x'*501,'x')]:
            with self.assertRaises(ConnectorError): await self.w.prepare(category,title,summary,content)
        self.assertEqual(self.session.posts,0)
    async def test_concurrent_commit_single_post_and_restart_receipt(self):
        p = await self.prepare()
        results = await asyncio.gather(*(self.w.commit(p['proposal_id']) for _ in range(3)))
        self.assertTrue(all(r['state']=='saved_verified' for r in results))
        self.assertEqual(self.session.posts,1)
        new = Writeback(self.session,Path(self.tmp.name))
        self.assertEqual((await new.receipt(p['proposal_id']))['state'],'saved_verified')
        self.assertEqual((await new.commit(p['proposal_id']))['state'],'saved_verified')
    async def test_expiry_or_account_switch_rejected(self):
        for kind in ('expiry','account','epoch'):
            p=await self.prepare()
            if kind=='expiry':self.clock[0]+=600
            if kind=='account':self.session.account='account-b'
            if kind=='epoch':self.session.epoch+=1
            self.assertEqual((await self.w.commit(p['proposal_id']))['state'],'rejected')
        self.assertEqual(self.session.posts,0)
    async def test_timeout_never_reposts_read_only_recovery(self):
        p=await self.prepare(); self.session.fail_post=True
        self.assertEqual((await self.w.commit(p['proposal_id']))['state'],'unknown')
        self.assertEqual((await self.w.commit(p['proposal_id']))['state'],'unknown')
        self.assertEqual(self.session.posts,1)
        self.assertEqual((await self.w.receipt(p['proposal_id']))['state'],'saved_verified')
        self.assertEqual(self.session.posts,1)
    async def test_mismatch_not_saved(self):
        p=await self.prepare();self.session.mismatch=True
        self.assertEqual((await self.w.commit(p['proposal_id']))['state'],'verification_failed')
    async def test_existing_target_and_unproven_contract_zero_post(self):
        p=await self.prepare();self.session.create_contract_verified=False
        self.assertEqual((await self.w.commit(p['proposal_id']))['state'],'blocked')
        self.session.create_contract_verified=True
        p=await self.prepare();self.session.page=p['preview']
        self.assertEqual((await self.w.commit(p['proposal_id']))['state'],'rejected')
        self.assertEqual(self.session.posts,0)
    async def test_restart_does_not_reactivate_prepared_proposal(self):
        p=await self.prepare();new=Writeback(self.session,Path(self.tmp.name))
        with self.assertRaises(ConnectorError):await new.commit(p['proposal_id'])
    async def test_expired_and_finalized_proposals_release_capacity(self):
        for _ in range(64):await self.prepare()
        with self.assertRaises(ConnectorError):await self.prepare()
        self.clock[0]+=600
        await self.prepare()
        self.session.create_contract_verified=False
        for _ in range(70):
            p=await self.prepare()
            await self.w.commit(p['proposal_id'])
        self.assertLessEqual(len(self.w.proposals),1)
