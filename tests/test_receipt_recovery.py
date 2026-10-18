"""Receipt recovery tests: delete-unknown must reconcile read-only, never rewrite."""
import json
import tempfile
import unittest
from pathlib import Path

from lingxi_connector.cloud import ConnectorError
from lingxi_connector.writeback import Writeback

TARGET = 'wiki/projects/lingxi-connector-recovery-1'


def marker(kind='test'):
    value = {'schema': 1, 'project': 'LX Memory', 'supersedes': [],
             'as_of': '2026-10-17T00:00:00+08:00', 'record_kind': kind}
    return '<!-- memory-project-record ' + json.dumps(value, ensure_ascii=False, sort_keys=True) + ' -->'


def page(path=TARGET, content=None, front_matter=None):
    return {'path': path,
            'front_matter': front_matter or {'title': 't', 'summary': 's', 'sources': [], 'related': []},
            'content': content if content is not None else marker() + '\n\nbody',
            'source': 'lingxi_cloud', 'content_scope': 'full', 'truncated': False,
            'updated': 1, 'retrieved_at': '2026-10-17T00:00:00+00:00'}


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class RecoverySession:
    epoch = 0
    account = 'account-a'
    create_contract_verified = True

    def __init__(self, content=None):
        self.pages = {TARGET: page(content=content)}
        self.deletes = 0
        self.posts = 0
        self.puts = 0
        self.reads = 0
        self.read_error = None
        self.delete_behaviour = 'lost_response'
        self.write_behaviour = 'ok'

    @property
    def writes(self):
        return self.deletes + self.posts + self.puts

    def write_enabled_for(self, account):
        return True

    async def identity(self):
        return self.account

    async def search(self, query, limit=5):
        return {'source': 'lingxi_cloud', 'total': 0, 'results': []}

    async def read(self, path, offset=0, max_chars=20000):
        self.reads += 1
        if self.read_error is not None:
            raise ConnectorError(self.read_error)
        if path not in self.pages:
            raise ConnectorError('PAGE_NOT_FOUND')
        return dict(self.pages[path])

    async def delete_page(self, path, expected_account=None, expected_epoch=None):
        self.deletes += 1
        if self.delete_behaviour == 'lost_response':
            del self.pages[path]
            raise ConnectorError('WRITE_OUTCOME_UNKNOWN')
        raise ConnectorError('TEMPORARY_UPSTREAM')

    async def create_page(self, payload, expected_account=None, expected_epoch=None):
        self.posts += 1
        self.pages[payload['path']] = page(path=payload['path'], content=payload['content'],
                                           front_matter=payload['front_matter'])
        if self.write_behaviour == 'lost_response':
            raise ConnectorError('WRITE_OUTCOME_UNKNOWN')
        return {}

    async def replace_page(self, payload, expected_account=None, expected_epoch=None):
        self.puts += 1
        self.pages[payload['path']] = page(path=payload['path'], content=payload['content'],
                                           front_matter=payload['front_matter'])
        if self.write_behaviour == 'lost_response':
            raise ConnectorError('WRITE_OUTCOME_UNKNOWN')
        return {}


class ReceiptRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = RecoverySession()
        self.w = Writeback(self.session, Path(self.tmp.name), clock=Clock())

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def _unknown_delete(self):
        proposal = await self.w.prepare_archive(TARGET)
        result = await self.w.commit(proposal['proposal_id'])
        self.assertEqual(result['state'], 'unknown')
        return proposal['proposal_id']

    async def test_delete_lost_response_is_reconciled_as_deleted(self):
        pid = await self._unknown_delete()
        receipt = await self.w.receipt(pid)
        self.assertEqual(receipt['state'], 'deleted_verified')
        self.assertEqual(self.session.deletes, 1)
        # Repeated receipt checks must not write again.
        again = await self.w.receipt(pid)
        self.assertEqual(again['state'], 'deleted_verified')
        self.assertEqual(self.session.deletes, 1)

    async def test_delete_not_applied_reports_still_exists(self):
        self.session.delete_behaviour = 'rejected'
        pid = await self._unknown_delete()
        writes = self.session.writes
        receipt = await self.w.receipt(pid)
        self.assertEqual(receipt['state'], 'unknown')
        self.assertEqual(receipt['error'], 'TARGET_STILL_EXISTS')
        self.assertEqual(self.session.writes, writes)

    async def test_read_errors_are_not_treated_as_absence(self):
        for code in ('AUTH_REQUIRED', 'PERMISSION_DENIED', 'RATE_LIMITED', 'NETWORK_ERROR'):
            self.session = RecoverySession()
            self.w = Writeback(self.session, Path(self.tmp.name), clock=Clock())
            pid = await self._unknown_delete()
            self.session.read_error = code
            writes = self.session.writes
            receipt = await self.w.receipt(pid)
            self.assertEqual(receipt['state'], 'unknown', code)
            self.assertNotEqual(receipt.get('state'), 'deleted_verified', code)
            self.assertEqual(self.session.writes, writes, code)

    async def test_account_or_epoch_change_blocks_reconciliation(self):
        pid = await self._unknown_delete()
        self.session.account = 'account-b'
        with self.assertRaises(ConnectorError) as raised:
            await self.w.receipt(pid)
        self.assertEqual(raised.exception.code, 'ACCOUNT_CHANGED')

        self.session = RecoverySession()
        self.w = Writeback(self.session, Path(self.tmp.name), clock=Clock())
        pid = await self._unknown_delete()
        self.session.epoch = 1
        with self.assertRaises(ConnectorError) as raised:
            await self.w.receipt(pid)
        self.assertEqual(raised.exception.code, 'ACCOUNT_CHANGED')

    async def test_create_unknown_recovery_still_works(self):
        proposal = await self.w.prepare('concepts', 't', 's', 'body')
        self.session.write_behaviour = 'lost_response'
        result = await self.w.commit(proposal['proposal_id'])
        self.assertEqual(result['state'], 'unknown')
        receipt = await self.w.receipt(proposal['proposal_id'])
        self.assertEqual(receipt['state'], 'saved_verified')

    async def test_replace_unknown_recovery_still_works(self):
        target = 'wiki/concepts/lingxi-connector-recovery-2'
        self.session.pages[target] = page(path=target, content='old')
        proposal = await self.w.prepare('concepts', 't', 's', 'new body', target_path=target)
        self.session.write_behaviour = 'lost_response'
        result = await self.w.commit(proposal['proposal_id'])
        self.assertEqual(result['state'], 'unknown')
        receipt = await self.w.receipt(proposal['proposal_id'])
        self.assertEqual(receipt['state'], 'saved_verified')


if __name__ == '__main__':
    unittest.main()
