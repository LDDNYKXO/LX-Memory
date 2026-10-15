import json
from pathlib import Path
import tempfile
import unittest
from lingxi_connector.setup import configure, InstallerError

class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.base=Path(self.tmp.name);self.home=self.base/'home';self.home.mkdir()
        self.root=self.base/'产品 路径';self.root.mkdir()
        self.python=self.root/'python.exe';self.python.write_text('fixture')
        source=self.root/'skills/lingxi-cloud-memory';source.mkdir(parents=True)
        (source/'SKILL.md').write_text('---\nname: lingxi-cloud-memory\n---\n规则',encoding='utf-8')
    def tearDown(self):self.tmp.cleanup()
    def test_json_preserves_other_servers_and_repeat_is_noop(self):
        p=self.home/'.workbuddy/mcp.json';p.parent.mkdir()
        p.write_text(json.dumps({'mcpServers':{'other':{'command':'existing'}},'extra':17}))
        report=configure(self.root,self.home,self.python,['WorkBuddy'],apply=True)
        self.assertEqual(json.loads(p.read_text(encoding='utf-8'))['mcpServers']['other'],{'command':'existing'})
        self.assertTrue(report['clients'][0]['configured'])
        before=p.read_bytes()
        second=configure(self.root,self.home,self.python,['WorkBuddy'],apply=True)
        self.assertEqual(before,p.read_bytes())
        self.assertFalse(second['clients'][0]['changed'])
    def test_toml_and_yaml_preserve_unrelated_data(self):
        p=self.home/'.codex/config.toml';p.parent.mkdir()
        p.write_text('model = "unchanged"\n[mcp_servers.other]\ncommand = "other"\n')
        y=self.home/'.dsh/profiles/desktop/cordis.patch.yml';y.parent.mkdir(parents=True)
        y.write_text('- id: other\n  name: unrelated\n  config: {value: 3}\n')
        original=p.read_bytes();yoriginal=y.read_bytes()
        report=configure(self.root,self.home,self.python,['Codex','DSH'],apply=True)
        self.assertTrue(p.read_bytes().startswith(original))
        self.assertTrue(y.read_bytes().startswith(yoriginal))
        self.assertTrue(all(c['configured'] for c in report['clients']))
    def test_preview_creates_nothing_and_conflict_fails_before_writes(self):
        preview=configure(self.root,self.home,self.python,['Codex','ZCode'],apply=False)
        self.assertFalse((self.home/'.codex').exists())
        self.assertFalse((self.root/'runtime').exists())
        p=self.home/'.workbuddy/mcp.json';p.parent.mkdir()
        p.write_text('{"mcpServers":{"lingxi-memory":{"command":"unrelated"}}}')
        with self.assertRaises(InstallerError):configure(self.root,self.home,self.python,['Codex','WorkBuddy'],apply=True)
        self.assertFalse((self.home/'.codex').exists())
    def test_invalid_config_and_unknown_client_fail_closed(self):
        p=self.home/'.zcode/cli/config.json';p.parent.mkdir(parents=True);p.write_text('invalid')
        with self.assertRaises(InstallerError):configure(self.root,self.home,self.python,['ZCode'],apply=True)
        self.assertEqual(p.read_text(),'invalid')
        with self.assertRaises(InstallerError):configure(self.root,self.home,self.python,['Other'],apply=True)
    def test_existing_foreign_skill_not_overwritten(self):
        p=self.home/'.agents/skills/lingxi-cloud-memory/SKILL.md';p.parent.mkdir(parents=True);p.write_text('user edit')
        with self.assertRaises(InstallerError):configure(self.root,self.home,self.python,['Codex'],apply=True)
        self.assertEqual(p.read_text(),'user edit')
    def test_dsh_js_extension_tag_is_supported(self):
        y=self.home/'.dsh/profiles/desktop/cordis.patch.yml';y.parent.mkdir(parents=True)
        original=("- id: session-query-sqlite\n  config:\n    path: !!js dshHomePath('session-index.sqlite')\n    openAt: first-search\n").encode('utf-8')
        y.write_bytes(original)
        report=configure(self.root,self.home,self.python,['DSH'],apply=True)
        self.assertTrue(report['clients'][0]['configured'])
        after=y.read_bytes()
        self.assertTrue(after.startswith(original))
        self.assertIn(b"!!js dshHomePath('session-index.sqlite')",after)
        self.assertIn(b'id: mcp-lingxi-memory',after)
        second=configure(self.root,self.home,self.python,['DSH'],apply=True)
        self.assertFalse(second['clients'][0]['changed'])
        self.assertEqual(after,y.read_bytes())
