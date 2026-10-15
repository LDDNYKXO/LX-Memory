import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
from lingxi_connector.setup import configure, InstallerError
from lingxi_connector.broker_client import call
from lingxi_connector.cloud import ConnectorError

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)
        self.home=self.base/'home';self.home.mkdir()
        self.root=self.product('a')
    def product(self,name):
        root=self.base/name;source=root/'skills/lingxi-cloud-memory';source.mkdir(parents=True)
        (source/'SKILL.md').write_text('skill',encoding='utf-8')
        return root
    def tearDown(self):self.tmp.cleanup()
    def test_partial_migration_retains_previous_owned_root(self):
        configure(self.root,self.home,self.root/'python.exe',['Codex','WorkBuddy'],True)
        second=self.product('b')
        configure(second,self.home,second/'python.exe',['Codex'],True)
        result=configure(second,self.home,second/'python.exe',['WorkBuddy'],True)
        self.assertTrue(result['clients'][0]['configured'])
    def test_journal_intent_exists_before_target_replace(self):
        original=Path.replace
        def failure(path,target):
            result=original(path,target)
            if path.name.startswith('config.toml.lingxi-'):raise OSError('simulated stop after target replacement')
            return result
        with patch.object(Path,'replace',failure):
            with self.assertRaises(InstallerError):configure(self.root,self.home,self.root/'python.exe',['Codex'],True)
        journals=list((self.root/'runtime/session').glob('setup-*.json'))
        self.assertEqual(len(journals),1)
        record=json.loads(journals[0].read_text(encoding='utf-8'))
        self.assertFalse(record['finished'])
        self.assertTrue(any(e['path']==str(self.home/'.codex/config.toml') for e in record['entries']))
    def test_enable_saving_timeout_is_never_automatically_replayed(self):
        directory=self.root/'runtime/session';directory.mkdir(parents=True)
        (directory/'connection.json').write_text(json.dumps({'port':12345,'token':'x'*32,'pid':123,'version':1}))
        opener=Mock();opener.open.side_effect=TimeoutError('local response unknown')
        with patch('lingxi_connector.broker_client.build_opener',return_value=opener),patch('lingxi_connector.broker_client.subprocess.Popen'),patch('lingxi_connector.broker_client.time.sleep'):
            with self.assertRaises(ConnectorError) as error:call(self.root,'enable-saving')
        self.assertEqual(error.exception.code,'WRITE_OUTCOME_UNKNOWN')
        self.assertEqual(opener.open.call_count,1)
    def test_existing_config_can_update_when_directory_denies_temp_creation(self):
        target=self.home/'.codex/config.toml';target.parent.mkdir();target.write_text('model = "keep"\n')
        original=Path.open
        def restricted(path,mode='r',*args,**kwargs):
            if path.name.startswith('config.toml.lingxi-') and mode=='xb':raise PermissionError('fixture temp creation denied')
            return original(path,mode,*args,**kwargs)
        with patch.object(Path,'open',restricted):
            result=configure(self.root,self.home,self.root/'python.exe',['Codex'],True)
        self.assertTrue(result['clients'][0]['configured'])
        self.assertIn('model = "keep"',target.read_text(encoding='utf-8'))
    def test_native_writer_after_python_file_permission_failure(self):
        target=self.home/'.codex/config.toml';target.parent.mkdir();target.write_text('model = "keep"\n')
        original=Path.open;calls=[]
        def restricted(path,mode='r',*args,**kwargs):
            if path.name.startswith('config.toml.lingxi-') or (path==target and mode=='r+b'):raise PermissionError('fixture Python write denied')
            return original(path,mode,*args,**kwargs)
        def native(path,raw,updated,root):
            self.assertEqual(path.read_bytes(),raw);calls.append(path)
            with original(path,'wb') as stream:stream.write(updated)
        with patch.object(Path,'open',restricted),patch('lingxi_connector.setup.native_write',native):
            result=configure(self.root,self.home,self.root/'python.exe',['Codex'],True)
        self.assertTrue(result['clients'][0]['configured'])
        self.assertEqual(calls,[target])
