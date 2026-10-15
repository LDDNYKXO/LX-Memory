import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from lingxi_connector.release import build, FILES, verify_manifest

class ReleaseTests(unittest.TestCase):
    def test_release_excludes_all_runtime_and_client_config(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'product';root.mkdir()
            for name in FILES:(root/name).write_text('fixture',encoding='utf-8')
            for name in ('vendor/python/python.exe','vendor/deps/package.py','src/lingxi_connector/module.py','skills/lingxi-cloud-memory/SKILL.md','runtime/session/connection.json','client-config.json'):
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('fixture')
            result=build(root,root/'dist/product.zip')
            with zipfile.ZipFile(result['path']) as z:
                names=z.namelist()
                self.assertFalse(any('/runtime/' in '/'+n or n.endswith('client-config.json') for n in names))
                self.assertIn('MANIFEST-SHA256.json',names)
                target=root/'unpacked';z.extractall(target)
            self.assertTrue(verify_manifest(target))
            (target/'run.py').write_text('changed')
            with self.assertRaises(ValueError):verify_manifest(target)
    def test_manifest_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/'MANIFEST-SHA256.json').write_text(json.dumps({'files':{'../outside':'0'*64}}))
            with self.assertRaises(ValueError):verify_manifest(root)
