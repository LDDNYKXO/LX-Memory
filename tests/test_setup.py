import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from lingxi_connector import setup as setup_module
from lingxi_connector.setup import configure, InstallerError, KNOWN_SKILLS

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
    def test_live_machine_skill_hashes_are_known_product_versions(self):
        # 2026-10-15 现网实测哈希：不加入白名单会让每台已安装机器重装/升级硬中断。
        self.assertIn('7cd301a94b16b80427123a4027a856e6db0a6a78a02e55dcd43a0f01750f4053',KNOWN_SKILLS)
        self.assertIn('b2a6a08685cb69bead347efb92a4eab3fd1889e43f4073902089aac3f388745e',KNOWN_SKILLS)
    def test_every_released_skill_version_stays_known(self):
        # 本轮已发布给现网的版本也必须常驻白名单，否则下一次升级会被判成“用户修改”而被拒绝。
        self.assertIn('5a17c7bcb8f051b4e985961cd7101bd1ed0c19df7f67ad377044510e9241ff65',KNOWN_SKILLS)
    def test_the_shipped_skill_version_is_whitelisted_for_the_next_upgrade(self):
        # 本轮真正安装出去的技能哈希也必须入册：否则它一被下一代技能替换，就被误判为用户修改。
        shipped=(Path(__file__).resolve().parents[1]/'skills/lingxi-cloud-memory/SKILL.md').read_bytes()
        self.assertIn(hashlib.sha256(shipped).hexdigest(),KNOWN_SKILLS)
    def test_known_historical_skill_version_upgrades_silently(self):
        p=self.home/'.agents/skills/lingxi-cloud-memory/SKILL.md';p.parent.mkdir(parents=True)
        older=b'---\nname: lingxi-cloud-memory\n---\nolder product rules'
        p.write_bytes(older)
        with patch.object(setup_module,'KNOWN_SKILLS',{hashlib.sha256(older).hexdigest()}):
            report=configure(self.root,self.home,self.python,['Codex'],apply=True)
        self.assertEqual(p.read_bytes(),(self.root/'skills/lingxi-cloud-memory/SKILL.md').read_bytes())
        self.assertFalse(report['skill_unknown_modification'])
    def test_unknown_skill_modification_reports_stable_code(self):
        p=self.home/'.agents/skills/lingxi-cloud-memory/SKILL.md';p.parent.mkdir(parents=True);p.write_text('user edit')
        with self.assertRaisesRegex(InstallerError,'^SKILL_UNKNOWN_MODIFICATION: '):
            configure(self.root,self.home,self.python,['Codex'],apply=True)
        self.assertEqual(p.read_text(),'user edit')
        self.assertFalse((self.home/'.codex').exists())
    def test_replace_skill_backs_up_and_reports(self):
        p=self.home/'.agents/skills/lingxi-cloud-memory/SKILL.md';p.parent.mkdir(parents=True);p.write_text('user edit')
        report=configure(self.root,self.home,self.python,['Codex'],apply=True,replace_skill=True)
        self.assertTrue(report['skill_unknown_modification'])
        self.assertEqual(p.read_bytes(),(self.root/'skills/lingxi-cloud-memory/SKILL.md').read_bytes())
        backup=Path(report['skill_backup'])
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_text(encoding='utf-8'),'user edit')
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
