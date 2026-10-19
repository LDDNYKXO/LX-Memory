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


class ContextToolTests(unittest.IsolatedAsyncioTestCase):
    async def tools(self):
        server = create_server(Path('.'))
        return {tool.name: tool for tool in await server.list_tools()}

    async def test_context_tool_is_registered_and_read_only(self):
        tools = await self.tools()
        self.assertIn('lingxi_memory_context', tools)
        self.assertTrue(tools['lingxi_memory_context'].annotations.read_only_hint)
        self.assertFalse(tools['lingxi_memory_context'].annotations.destructive_hint)

    async def test_validate_tool_is_registered_and_read_only(self):
        tools = await self.tools()
        self.assertIn('lingxi_memory_validate', tools)
        self.assertTrue(tools['lingxi_memory_validate'].annotations.read_only_hint)
        self.assertFalse(tools['lingxi_memory_validate'].annotations.destructive_hint)

    async def test_archive_tool_is_registered_and_destructive(self):
        tools = await self.tools()
        self.assertIn('lingxi_memory_archive', tools)
        self.assertFalse(tools['lingxi_memory_archive'].annotations.read_only_hint)
        self.assertTrue(tools['lingxi_memory_archive'].annotations.destructive_hint)

    async def test_thirteen_tools_total_and_the_original_six_are_intact(self):
        tools = await self.tools()
        self.assertEqual(len(tools), 13)
        self.assertTrue({'lingxi_memory_status', 'lingxi_memory_search', 'lingxi_memory_read',
                         'lingxi_memory_prepare', 'lingxi_memory_commit',
                         'lingxi_memory_receipt', 'lingxi_memory_context',
                         'lingxi_memory_validate', 'lingxi_memory_archive',
                         'lingxi_memory_browse', 'lingxi_memory_export',
                         'lingxi_memory_backup', 'lingxi_memory_backup_verify'} <= set(tools))

    async def test_browse_is_read_only_and_export_writes_locally(self):
        tools = await self.tools()
        self.assertTrue(tools['lingxi_memory_browse'].annotations.read_only_hint)
        self.assertFalse(tools['lingxi_memory_export'].annotations.read_only_hint)
        self.assertFalse(tools['lingxi_memory_export'].annotations.destructive_hint)
        self.assertTrue(tools['lingxi_memory_backup_verify'].annotations.read_only_hint)
        self.assertFalse(tools['lingxi_memory_backup'].annotations.read_only_hint)

    async def test_context_tool_schema_matches_the_broker_whitelist(self):
        tools = await self.tools()
        schema = tools['lingxi_memory_context'].input_schema
        self.assertEqual(set(schema['properties']), {'request', 'project', 'cwd', 'remote', 'request_id', 'view', 'snapshot_id', 'cursor'})
        self.assertEqual(schema.get('required'), ['request'])

    async def test_context_tool_calls_the_context_operation(self):
        with patch('lingxi_connector.mcp_server.call', return_value={'state': 'complete'}) as called:
            server = create_server(Path('.'))
            await server.call_tool('lingxi_memory_context', {'request': '继续这个项目', 'project': '灵犀记忆'})
        self.assertEqual(called.call_args.args[1], 'context')
        self.assertEqual(called.call_args.args[2], {'request': '继续这个项目', 'project': '灵犀记忆',
                                                    'cwd': '', 'remote': '', 'request_id': ''})

    async def test_context_tool_forwards_the_repository_identity(self):
        with patch('lingxi_connector.mcp_server.call', return_value={'state': 'complete'}) as called:
            server = create_server(Path('.'))
            await server.call_tool('lingxi_memory_context', {'request': '继续', 'remote': 'github.com/o/r'})
        self.assertEqual(called.call_args.args[2]['remote'], 'github.com/o/r')

    async def test_instructions_describe_the_background_entry(self):
        server = create_server(Path('.'))
        self.assertIn('lingxi_memory_context', server.instructions)
        self.assertIn('避免重复检索', server.instructions)
