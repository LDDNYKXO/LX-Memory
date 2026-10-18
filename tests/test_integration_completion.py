import json
import tempfile
import unittest
from pathlib import Path
from lingxi_connector import integration
from lingxi_connector.setup import InstallerError
from lingxi_connector.broker import Broker
from test_broker import FakeSession


class RuleTransactionTests(unittest.TestCase):
    def test_known_rule_upgrade_preserves_bom_and_foreign_bytes_and_rolls_back(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            home, root = Path(tmp) / 'home', Path(tmp) / 'product'
            folder = root / 'integrations'; folder.mkdir(parents=True)
            (folder / 'resume-rules.md').write_text('New rules.\n', encoding='utf-8')
            old = (integration.RULE_START + '\nOld rules.\n' + integration.RULE_END + '\n').encode()
            (folder / 'rules-history.json').write_text(json.dumps({
                'schema': 1, 'block_sha256': [hashlib.sha256(old).hexdigest()]}))
            target = home / '.dsh/AGENTS.md'; target.parent.mkdir(parents=True)
            before = b'\xef\xbb\xbfKeep original\r\n' + old + b'Keep suffix\r\n'
            target.write_bytes(before)
            report = integration.configure_rules(root, home, ['DSH'], apply=True)
            self.assertIn(b'New rules.', target.read_bytes())
            self.assertTrue(target.read_bytes().startswith(b'\xef\xbb\xbfKeep original\r\n'))
            self.assertTrue(target.read_bytes().endswith(b'Keep suffix\r\n'))
            integration.rollback_rules(root, home, report['journal'])
            self.assertEqual(target.read_bytes(), before)

    def test_unknown_rule_upgrade_refuses_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, root = Path(tmp) / 'home', Path(tmp) / 'product'
            folder = root / 'integrations'; folder.mkdir(parents=True)
            (folder / 'resume-rules.md').write_text('New rules.\n', encoding='utf-8')
            target = home / '.dsh/AGENTS.md'; target.parent.mkdir(parents=True)
            before = (integration.RULE_START + '\nUser edited\n' + integration.RULE_END + '\n').encode()
            target.write_bytes(before)
            with self.assertRaisesRegex(InstallerError, 'RULE_UNKNOWN_MODIFICATION'):
                integration.configure_rules(root, home, ['DSH'], apply=True)
            self.assertEqual(target.read_bytes(), before)

    def test_dsh_lifecycle_file_uri_is_recognized_with_nonascii_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, root = Path(tmp) / 'home', Path(tmp) / '灵犀 product'
            adapter = root / 'integrations/dsh/index.mjs'
            adapter.parent.mkdir(parents=True); adapter.write_text('// fixture')
            target = home / '.dsh/profiles/desktop/cordis.patch.yml'
            target.parent.mkdir(parents=True); target.write_text('- insert: []\n')
            integration.configure_lifecycle(root, home, Path('C:/Python/python.exe'), ['DSH'], apply=True)
            self.assertTrue(integration.capability_matrix(home, root)['clients']['DSH']['lifecycle_configured'])

    def test_workbuddy_hook_preserves_other_settings_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, root = Path(tmp) / 'home', Path(tmp) / 'product'
            target = home / '.workbuddy/settings.json'
            target.parent.mkdir(parents=True)
            before = b'{"theme":"dark","hooks":{"Stop":[{"hooks":[{"type":"command","command":"keep-me"}]}]}}\n'
            target.write_bytes(before)
            report = integration.configure_lifecycle(root, home, Path('C:/Python/python.exe'), ['WorkBuddy'], apply=True)
            data = json.loads(target.read_bytes())
            self.assertEqual(data['theme'], 'dark')
            self.assertEqual(data['hooks']['Stop'][0]['hooks'][0]['command'], 'keep-me')
            self.assertEqual(len(data['hooks']['UserPromptSubmit']), 1)
            self.assertEqual(integration.configure_lifecycle(root, home, Path('C:/Python/python.exe'), ['WorkBuddy'], apply=True)['changed'], 0)
            integration.rollback_rules(root, home, report['journal'])
            self.assertEqual(target.read_bytes(), before)

    def test_real_dsh_insert_patch_is_visible_to_production_inspection(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, root = Path(tmp) / 'home', Path(tmp) / 'product'
            path = home / '.dsh/profiles/desktop/cordis.patch.yml'
            path.parent.mkdir(parents=True)
            path.write_text('- insert:\n  - id: mcp-lingxi-memory\n    name: "@deepseek-ai/dsh-mcp-client"\n    config:\n      serverName: lingxi-memory\n      args: ["-B", "' + (root / 'run.py').as_posix() + '", "mcp"]\n', encoding='utf-8')
            report = integration.verify_ownership({'DSH': path}, [root])
            self.assertEqual(report['clients']['DSH']['state'], 'owned')

    def test_user_rules_survive_install_and_exact_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'home'
            root = Path(tmp) / 'product'
            home.mkdir(); root.mkdir()
            source = root / 'integrations' / 'resume-rules.md'
            source.parent.mkdir(); source.write_text('Use the shared context tool.\n', encoding='utf-8')
            target = home / '.dsh' / 'AGENTS.md'
            target.parent.mkdir(); before = b'Existing user rules\r\n'
            target.write_bytes(before)
            report = integration.configure_rules(root, home, ['DSH'], apply=True)
            self.assertTrue(target.read_bytes().startswith(before))
            self.assertIn(b'Use the shared context tool.', target.read_bytes())
            again = integration.configure_rules(root, home, ['DSH'], apply=True)
            self.assertEqual(again['changed'], 0)
            integration.rollback_rules(root, home, report['journal'])
            self.assertEqual(target.read_bytes(), before)

    def test_rollback_refuses_to_clobber_later_user_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, root = Path(tmp) / 'home', Path(tmp) / 'product'
            home.mkdir(); root.mkdir()
            source = root / 'integrations' / 'resume-rules.md'
            source.parent.mkdir(); source.write_text('Use context.\n', encoding='utf-8')
            report = integration.configure_rules(root, home, ['DSH'], apply=True)
            target = home / '.dsh' / 'AGENTS.md'
            target.write_text('User changed this after install', encoding='utf-8')
            with self.assertRaises(InstallerError):
                integration.rollback_rules(root, home, report['journal'])
            self.assertEqual(target.read_text(), 'User changed this after install')

    def test_new_rule_file_is_removed_on_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, root = Path(tmp) / 'home', Path(tmp) / 'product'
            home.mkdir(); root.mkdir()
            source = root / 'integrations' / 'resume-rules.md'
            source.parent.mkdir(); source.write_text('Use context.\n', encoding='utf-8')
            report = integration.configure_rules(root, home, ['ZCode'], apply=True)
            integration.rollback_rules(root, home, report['journal'])
            self.assertFalse((home / '.zcode' / 'AGENTS.md').exists())


class TurnIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_requests_without_a_turn_id_cannot_share_old_background(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker = Broker(Path(tmp)); broker.session = FakeSession()
            first = await broker.context({'request': '继续', 'project': '灵犀记忆'})
            second = await broker.context({'request': '继续', 'project': '灵犀记忆'})
            self.assertNotEqual(first['request_id'], second['request_id'])
            self.assertFalse(second.get('cached', False))
