import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from lingxi_connector.integration import (CAPABILITY_FILE, capability_matrix, declared_matrix,
                                          verify_ownership)
from lingxi_connector.setup import InstallerError


def snapshot(directory):
    """Read-only fingerprint: relative path -> sha256 for every file below `directory`."""
    result = {}
    for path in sorted(Path(directory).rglob('*')):
        if path.is_file():
            result[str(path.relative_to(directory)).replace('\\', '/')] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


class CapabilityMatrixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / 'home'
        self.home.mkdir()
        self.root = self.base / 'product'
        self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_matrix_uses_the_two_confirmed_mechanisms_only(self):
        matrix = capability_matrix(self.home, self.root)
        self.assertEqual({row['mechanism'] for row in matrix['clients'].values()},
                         {'mcp_tool', 'rules_injection', 'lifecycle'})

    def test_zcode_is_mcp_and_codex_and_dsh_are_rules_injection(self):
        clients = capability_matrix(self.home, self.root)['clients']
        self.assertEqual(clients['ZCode']['mechanism'], 'mcp_tool')
        self.assertEqual(clients['Codex']['mechanism'], 'rules_injection')
        self.assertEqual(clients['DSH']['mechanism'], 'lifecycle')

    def test_workbuddy_skill_is_explicit_and_never_auto(self):
        clients = capability_matrix(self.home, self.root)['clients']
        self.assertFalse(clients['WorkBuddy']['auto_bootstrap'])

    def test_nothing_is_configured_or_natively_verified_by_default(self):
        clients = capability_matrix(self.home, self.root)['clients']
        self.assertFalse(any(row['configured'] for row in clients.values()))
        self.assertFalse(any(row['verified_native'] for row in clients.values()))

    def test_configured_requires_our_own_root_in_the_host_config(self):
        config = self.home / '.zcode/cli/config.json'
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({'mcp': {'servers': {'lingxi-memory': {
            'command': 'python', 'args': ['-B', str(self.root / 'run.py'), 'mcp']}}}}), encoding='utf-8')
        self.assertTrue(capability_matrix(self.home, self.root)['clients']['ZCode']['configured'])
        self.assertFalse(capability_matrix(self.home, self.base / 'other')['clients']['ZCode']['configured'])

    def test_a_sibling_root_sharing_the_prefix_is_not_configured(self):
        # `…/product-backup` must not be adopted as `…/product`: only a path that really lives
        # under the root counts, never a plain string prefix.
        config = self.home / '.zcode/cli/config.json'
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({'mcp': {'servers': {'lingxi-memory': {
            'command': 'python', 'args': ['-B', str(self.base / 'product-backup' / 'run.py'), 'mcp']}}}}),
            encoding='utf-8')
        self.assertFalse(capability_matrix(self.home, self.root)['clients']['ZCode']['configured'])

    def test_rules_injection_host_is_configured_by_its_instruction_file(self):
        instruction = self.home / '.dsh/AGENTS.md'
        instruction.parent.mkdir(parents=True)
        instruction.write_text('产品嵌入块：lingxi-memory 先读背景', encoding='utf-8')
        clients = capability_matrix(self.home, self.root)['clients']
        self.assertFalse(clients['DSH']['configured'])
        self.assertFalse(clients['DSH']['mcp_configured'])
        self.assertTrue(clients['DSH']['instruction_present'])

    def test_matrix_is_read_only(self):
        before = snapshot(self.home)
        capability_matrix(self.home, self.root)
        self.assertEqual(snapshot(self.home), before)

    def test_declared_matrix_matches_the_runtime_classification(self):
        declared = declared_matrix()
        runtime = capability_matrix(self.home, self.root)['clients']
        self.assertEqual(set(declared['clients']), set(runtime))
        for name, row in declared['clients'].items():
            self.assertEqual(row['mechanism'], runtime[name]['mechanism'])
            self.assertEqual(row['auto_bootstrap'], runtime[name]['auto_bootstrap'])

    def test_committed_matrix_is_path_free_and_in_sync(self):
        self.assertTrue(CAPABILITY_FILE.is_file(), str(CAPABILITY_FILE))
        text = CAPABILITY_FILE.read_text(encoding='utf-8')
        self.assertNotIn('C:\\Users', text)
        committed = json.loads(text)
        declared = declared_matrix()
        self.assertEqual(set(committed['clients']), set(declared['clients']))
        for name, row in declared['clients'].items():
            self.assertEqual(committed['clients'][name]['mechanism'], row['mechanism'])
            self.assertEqual(committed['clients'][name]['auto_bootstrap'], row['auto_bootstrap'])
            self.assertEqual(committed['clients'][name]['instruction_path'], row['instruction_path'])


class VerifyOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / 'home'
        self.home.mkdir()
        self.root = self.base / 'product'
        self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def zcode_config(self, args):
        path = self.home / '.zcode/cli/config.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'mcp': {'servers': {'lingxi-memory': {'command': 'python', 'args': args}}}}),
                        encoding='utf-8')
        return path

    def test_unknown_format_stops_instead_of_rewriting(self):
        path = self.home / '.codex/config.toml'
        path.parent.mkdir(parents=True)
        path.write_text('%%%not toml%%%', encoding='utf-8')
        with self.assertRaises(InstallerError):
            verify_ownership({'Codex': path}, [str(self.root)])
        self.assertEqual(path.read_text(encoding='utf-8'), '%%%not toml%%%')

    def test_absent_config_is_reported_without_creating_it(self):
        report = verify_ownership({'Codex': self.home / '.codex/config.toml'}, [str(self.root)])
        self.assertEqual(report['clients']['Codex']['state'], 'absent')
        self.assertFalse((self.home / '.codex').exists())

    def test_owned_connector_is_reported_as_already_configured(self):
        path = self.zcode_config(['-B', str(self.root / 'run.py'), 'mcp'])
        report = verify_ownership({'ZCode': path}, [str(self.root)])
        self.assertEqual(report['clients']['ZCode']['state'], 'owned')
        self.assertTrue(report['clients']['ZCode']['already_configured'])

    def test_foreign_connector_is_reported_and_never_adopted(self):
        path = self.zcode_config(['-B', str(self.base / 'elsewhere' / 'run.py'), 'mcp'])
        report = verify_ownership({'ZCode': path}, [str(self.root)])
        self.assertEqual(report['clients']['ZCode']['state'], 'foreign')
        self.assertFalse(report['clients']['ZCode']['already_configured'])

    def test_a_sibling_root_sharing_the_prefix_stays_foreign(self):
        path = self.zcode_config(['-B', str(self.base / 'product-backup' / 'run.py'), 'mcp'])
        report = verify_ownership({'ZCode': path}, [str(self.root)])
        self.assertEqual(report['clients']['ZCode']['state'], 'foreign')
        self.assertFalse(report['clients']['ZCode']['already_configured'])

    def test_unrelated_config_is_preserved_and_counted(self):
        path = self.home / '.workbuddy/mcp.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'mcpServers': {'other': {'command': 'existing'}}, 'extra': 17}), encoding='utf-8')
        report = verify_ownership({'WorkBuddy': path}, [str(self.root)])
        self.assertEqual(report['clients']['WorkBuddy']['state'], 'unrelated-only')
        self.assertEqual(report['clients']['WorkBuddy']['preserved'], 1)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['extra'], 17)

    def test_dsh_js_extension_tag_is_tolerated(self):
        path = self.home / '.dsh/profiles/desktop/cordis.patch.yml'
        path.parent.mkdir(parents=True)
        path.write_text("- id: other\n  config:\n    path: !!js dshHomePath('x.sqlite')\n", encoding='utf-8')
        report = verify_ownership({'DSH': path}, [str(self.root)])
        self.assertEqual(report['clients']['DSH']['state'], 'unrelated-only')

    def test_verify_never_writes(self):
        path = self.zcode_config(['-B', str(self.root / 'run.py'), 'mcp'])
        before = snapshot(self.home)
        verify_ownership({'ZCode': path}, [str(self.root)])
        self.assertEqual(snapshot(self.home), before)


SKILL_FILE = Path(__file__).resolve().parents[1] / 'skills' / 'lingxi-cloud-memory' / 'SKILL.md'
LIVE_SKILL_SHA256 = '7cd301a94b16b80427123a4027a856e6db0a6a78a02e55dcd43a0f01750f4053'


class SkillContractTests(unittest.TestCase):
    def text(self):
        return SKILL_FILE.read_text(encoding='utf-8')

    def test_skill_declares_the_unified_background_entry_first(self):
        text = self.text()
        self.assertIn('lingxi_memory_context', text)
        self.assertLess(text.index('lingxi_memory_context'), text.index('lingxi_memory_search'))

    def test_skill_handles_every_reported_state(self):
        text = self.text()
        for state in ('complete', 'partial', 'project_ambiguous', 'conflict', 'service_unavailable'):
            self.assertIn(state, text)

    def test_skill_forbids_repeating_the_same_round_retrieval(self):
        text = self.text()
        self.assertIn('同轮', text)
        self.assertIn('不重复', text)
        self.assertIn('cached', text)

    def test_skill_keeps_a_fallback_for_an_older_runtime(self):
        text = self.text()
        self.assertIn('旧版本', text)
        self.assertIn('lingxi_memory_status', text)
        self.assertIn('lingxi_memory_read', text)

    def test_skill_keeps_the_write_authorization_boundary(self):
        text = self.text()
        for marker in ('lingxi_memory_prepare', 'lingxi_memory_commit', 'lingxi_memory_receipt',
                       'saved_verified', '不新建提案重发'):
            self.assertIn(marker, text)

    def test_this_release_actually_changes_the_live_skill(self):
        # 现场版本仍在 KNOWN_SKILLS 里（见 test_setup），这里确认本轮真的产出了新版本，
        # 否则"现网可静默升级"这条路径根本没被走到。
        self.assertNotEqual(hashlib.sha256(SKILL_FILE.read_bytes()).hexdigest(), LIVE_SKILL_SHA256)


ADAPTERS_FILE = Path(__file__).resolve().parents[1] / 'CLIENT-ADAPTERS.md'


class AdapterDocumentTests(unittest.TestCase):
    def rows(self):
        """Parse the adapter table: client -> cells of one markdown row."""
        text = ADAPTERS_FILE.read_text(encoding='utf-8')
        declared = declared_matrix()['clients']
        found = {}
        for line in text.splitlines():
            if not line.startswith('|'):
                continue
            cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
            if cells and cells[0] in declared:
                found[cells[0]] = cells
        return text, declared, found

    def test_every_declared_client_has_exactly_one_row(self):
        text, declared, found = self.rows()
        self.assertEqual(set(found), set(declared))

    def test_mechanism_trigger_and_entry_match_the_capability_matrix(self):
        text, declared, found = self.rows()
        for name, row in declared.items():
            cells = found[name]
            self.assertEqual(cells[1].strip('`'), row['mechanism'], name)
            self.assertEqual(cells[2], '是' if row['auto_bootstrap'] else '否', name)
            if row['mechanism'] == 'lifecycle':
                self.assertIn('agent/pre-step', cells[3], name)
            else:
                self.assertEqual(cells[3].strip('`'), row['instruction_path'], name)

    def test_every_row_states_evidence_and_a_real_limit(self):
        text, declared, found = self.rows()
        for name, cells in found.items():
            self.assertEqual(len(cells), 7, name)
            self.assertTrue(cells[5], name)     # 已验收证据：没有也要写“无”
            self.assertTrue(cells[6], name)     # 真实限制必须写明

    def test_document_never_claims_unproven_acceptance(self):
        text, declared, found = self.rows()
        self.assertNotIn('已支持', text)
        self.assertNotIn('已验收通过', text)
        self.assertIn('配置存在不能代替客户端原生调用验收', text)
        self.assertIn('无（本轮未做原生验收）', text)


if __name__ == '__main__':
    unittest.main()
