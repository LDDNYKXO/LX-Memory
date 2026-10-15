import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from lingxi_connector.mcp_server import create_server
from lingxi_connector.broker import Broker

class InterfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_annotations_and_explicit_commit(self):
        server = create_server(Path('.'))
        tools = {t.name:t for t in await server.list_tools()}
        self.assertIn('lingxi_memory_prepare',tools)
        self.assertFalse(tools['lingxi_memory_commit'].annotations.read_only_hint)
        self.assertFalse(tools['lingxi_memory_commit'].annotations.idempotent_hint)
        self.assertTrue(tools['lingxi_memory_receipt'].annotations.read_only_hint)
    async def test_broker_exposes_identity_boolean_not_user_id(self):
        with tempfile.TemporaryDirectory() as d:
            broker=Broker(Path(d))
            self.assertTrue(hasattr(broker,'writeback'))
            broker.session.identity=AsyncMock(return_value='private-user-id')
            status=await broker.writeback_status()
            self.assertTrue(status['account_verified'])
            self.assertFalse(status['write_enabled'])
            self.assertNotIn('private-user-id',str(status))
