import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

class LogoutCLITests(unittest.TestCase):
    def test_logout_after_stopped_broker_clears_saved_login_without_browser(self):
        # Separate product runtime; never clear a user's actual login.
        import shutil
        root = Path(__file__).resolve().parents[1]
        test_tmp = root / 'runtime' / 'test-tmp'
        test_tmp.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_tmp) as d:
            product = Path(d)
            assert product.resolve().is_relative_to(test_tmp.resolve())
            shutil.copyfile(root / 'run.py', product / 'run.py')
            shutil.copytree(root / 'src', product / 'src')
            runtime = product / 'runtime'
            runtime.mkdir()
            # Depend on original private dependency tree via PYTHONPATH, not a second install.
            import os
            env = {**os.environ, 'PYTHONDONTWRITEBYTECODE':'1','PYTHONIOENCODING':'utf-8', 'PYTHONPATH':str(root / 'runtime' / 'deps')}
            interpreter = sys.executable
            if Path(sys.executable).resolve() == (root/'vendor/python/python.exe').resolve():
                shutil.copytree(root/'vendor',product/'vendor')
                interpreter=str(product/'vendor/python/python.exe')
            session = runtime / 'session'
            (session / 'profile').mkdir(parents=True)
            (session / 'profile' / 'synthetic.txt').write_text('not credentials')
            (session / 'remember.json').write_text('{"consent":true}')
            result = subprocess.run([interpreter,'-B',str(product / 'run.py'),'logout'],env=env,capture_output=True,text=True,encoding='utf-8',timeout=30)
            try:
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse((session / 'profile').exists())
                self.assertFalse((session / 'remember.json').exists())
                self.assertEqual(json.loads(result.stdout)['state'], 'NOT_LOGGED_IN')
            finally:
                subprocess.run([interpreter,'-B',str(product / 'run.py'),'stop'],env=env,capture_output=True,timeout=30)
