import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from unittest.mock import patch
import base64
import hashlib
import contextlib
import io
import os
import shutil
import subprocess
import sys
import re

ROOT = Path(__file__).resolve().parents[1]


class AgentInstallTests(unittest.TestCase):
    def installer(self):
        self.assertTrue((ROOT / 'agent_install.py').is_file(), 'Agent installation entrypoint is missing')
        spec = importlib.util.spec_from_file_location('agent_install', ROOT / 'agent_install.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_connected_reuses_session_without_login(self):
        module = self.installer()
        broker = Mock(return_value={'state': 'CONNECTED', 'cloud_available': True})
        result = module.check_connection(Path('product'), broker, login=True, remember=True)
        self.assertEqual(result['action'], 'ready')
        self.assertEqual([c.args[1] for c in broker.call_args_list], ['status'])

    def test_login_is_only_opened_for_missing_auth(self):
        module = self.installer()
        for state in ('NOT_LOGGED_IN', 'AUTH_REQUIRED'):
            with self.subTest(state=state):
                broker = Mock(side_effect=[{'state': state}, {'state': 'LOGIN_IN_PROGRESS'}])
                result = module.check_connection(Path('product'), broker, login=True, remember=True)
                self.assertEqual(result['action'], 'finish_login')
                self.assertEqual(broker.call_args_list[1].args[2], {'remember': True})
        broker = Mock(return_value={'state': 'AUTH_REQUIRED'})
        self.assertEqual(module.check_connection(Path('product'), broker)['action'], 'login_required')
        self.assertEqual(broker.call_count, 1)

    def test_network_and_permission_errors_do_not_trigger_login(self):
        module = self.installer()
        for state in ('NETWORK_ERROR', 'UNKNOWN', 'PERMISSION_DENIED', 'LOGIN_IN_PROGRESS'):
            broker = Mock(return_value={'state': state, 'error': state})
            module.check_connection(Path('product'), broker, login=True)
            self.assertEqual(broker.call_count, 1)

    def test_existing_install_is_selected_without_copying_sessions(self):
        module = self.installer()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            product = home / 'old product'
            (product / 'src/lingxi_connector').mkdir(parents=True)
            (product / 'run.py').write_text('entry')
            (product / 'src/lingxi_connector/broker_client.py').write_text('broker')
            python = home / 'python.exe'
            python.write_text('runtime')
            registry = home / '.lingxi-memory-connector/install.json'
            registry.parent.mkdir()
            registry.write_text(json.dumps({'product': 'lingxi-memory-connector', 'root': str(product), 'python': str(python)}))
            self.assertEqual(module.existing_install(home), (product.resolve(), python.resolve()))
            registry.write_text('{broken')
            with self.assertRaises(module.InstallError):
                module.existing_install(home)

    def test_download_plan_excludes_runtime_and_refuses_traversal(self):
        module = self.installer()
        self.assertTrue(module.source_file('src/lingxi_connector/cloud.py'))
        self.assertTrue(module.source_file('skills/lingxi-cloud-memory/SKILL.md'))
        for path in ('runtime/session/remember.json', 'vendor/python/python.exe', '../run.py', 'src/../run.py', 'src/lingxi_connector/secret.json'):
            self.assertFalse(module.source_file(path), path)

    def test_source_file_allows_first_round_deliverables(self):
        module = self.installer()
        for path in ('CLIENT-ADAPTERS.md', 'integrations/README.md', 'integrations/capability-matrix.json', 'integrations/rulesync/rulesync.json'):
            self.assertTrue(module.source_file(path), path)
        for path in ('integrations/a/b/c/d.json', 'integrations/tool.ps1', 'integrations/x.exe', 'integrations/.git/config', 'integrations/build/package-lock.json', 'integrations/build/package.json'):
            self.assertFalse(module.source_file(path), path)

    def test_error_payload_is_a_stable_code_with_detail(self):
        from lingxi_connector.setup import InstallerError
        module = self.installer()
        payload = module.error_payload(InstallerError('SKILL_UNKNOWN_MODIFICATION: 技能含未知修改，未覆盖'))
        self.assertEqual(payload['error'], 'SKILL_UNKNOWN_MODIFICATION')
        self.assertIn('技能含未知修改', payload['detail'])
        self.assertEqual(module.error_payload(InstallerError('技能有用户修改'))['error'], 'SETUP_CONFIGURATION_FAILED')
        self.assertEqual(module.error_payload(module.InstallError('GITHUB_RATE_LIMITED: 限流'))['error'], 'GITHUB_RATE_LIMITED')
        self.assertEqual(module.error_payload(OSError('disk'))['error'], 'SETUP_CONFIGURATION_FAILED')

    def test_run_reports_installer_error_without_traceback(self):
        module = self.installer()
        from lingxi_connector.setup import InstallerError
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home / 'product'
            (root / 'src/lingxi_connector').mkdir(parents=True)
            (root / 'run.py').write_text('pass')
            (root / 'src/lingxi_connector/setup.py').write_text('pass')
            with patch.object(module.Path, 'home', return_value=home), \
                 patch.object(module, 'configure_clients', side_effect=InstallerError('SKILL_UNKNOWN_MODIFICATION: 技能含未知修改，未覆盖')), \
                 contextlib.redirect_stderr(io.StringIO()) as error_output:
                code = module.run(['--source', str(root), '--apply', '--clients', 'Codex'])
        self.assertEqual(code, 2)
        payload = json.loads(error_output.getvalue())
        self.assertEqual(payload['error'], 'SKILL_UNKNOWN_MODIFICATION')
        self.assertNotIn('Traceback', error_output.getvalue())

    def test_generic_install_exports_configuration_without_editing_unknown_client(self):
        module = self.installer()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home / 'product'
            root.mkdir()
            result = module.configure_clients(root, home, Path('python.exe'), ['Generic'], apply=True)
            self.assertEqual(result['mcpServers']['lingxi-memory']['args'][-1], 'mcp')
            registry = json.loads((home / '.lingxi-memory-connector/install.json').read_text())
            self.assertEqual(registry['product'], 'lingxi-memory-connector')
            self.assertFalse((home / '.codex').exists())

    def test_uninstalled_check_does_not_download_or_create_directories(self):
        module = self.installer()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with patch.object(module.Path, 'home', return_value=home), patch.object(module, 'download_sources') as download, contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(module.main(['--check']), 3)
            self.assertEqual(json.loads(output.getvalue())['state'], 'NOT_INSTALLED')
            download.assert_not_called()
            self.assertEqual(list(home.iterdir()), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows PowerShell entrypoint')
    def test_single_powershell_entrypoint_checks_existing_install_without_github(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            product = home / 'existing product'
            product.mkdir()
            (product / 'run.py').write_text('import json,sys; assert sys.argv[1:]==["status"], "Connected session must not reopen login"; print(json.dumps({"state":"CONNECTED","cloud_available":True}))', encoding='utf-8')
            registry = home / '.lingxi-memory-connector/install.json'
            registry.parent.mkdir()
            registry.write_text(json.dumps({'product': 'lingxi-memory-connector', 'root': str(product), 'python': sys.executable}))
            entrypoint = home / 'Install-Agent.ps1'
            shutil.copyfile(ROOT / 'Install-Agent.ps1', entrypoint)
            quoted = str(entrypoint).replace("'", "''")
            # All network is disabled by the harness. No private-repository access is needed.
            command = "function Invoke-RestMethod { throw 'TEST_NETWORK_DISABLED' }; function Get-Command { return $null }; & '" + quoted + "' -Check -Login"
            process = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', command], env={**os.environ, 'USERPROFILE': str(home)}, capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(json.loads(process.stdout)['connection']['action'], 'ready')

    def test_download_verifies_blob_before_writing_and_pins_all_reads(self):
        module = self.installer()
        commit = 'a' * 40
        names = sorted(module.ROOT_FILES | {'src/lingxi_connector/setup.py', 'src/lingxi_connector/broker.py', 'skills/lingxi-cloud-memory/SKILL.md'})
        raw = b'public source'
        sha = hashlib.sha1(b'blob 13\0' + raw).hexdigest()
        tree = {'tree': [{'type': 'blob', 'path': name, 'sha': sha, 'mode': '100644'} for name in names]}
        calls = []
        def response(endpoint):
            calls.append(endpoint)
            if '/commits/' in endpoint:
                return {'sha': commit}
            if '/git/trees/' in endpoint:
                return tree
            return {'encoding': 'base64', 'content': base64.b64encode(raw).decode()}
        with tempfile.TemporaryDirectory() as directory, patch.object(module, 'github_json', side_effect=response):
            root = Path(directory)
            self.assertEqual(module.download_sources(root, 'main'), commit)
            self.assertEqual((root / 'run.py').read_bytes(), raw)
            self.assertTrue(all(call.endswith('?ref=' + commit) for call in calls if '/contents/' in call))
        with tempfile.TemporaryDirectory() as directory, patch.object(module, 'github_json', side_effect=response):
            tree['tree'][0]['sha'] = '0' * 40
            with self.assertRaisesRegex(module.InstallError, 'SOURCE_HASH_MISMATCH'):
                module.download_sources(Path(directory), 'main')
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_public_source_download_never_uses_github_credentials(self):
        module = self.installer()
        response = contextlib.nullcontext(io.StringIO('{"sha":"public-source"}'))
        with patch('shutil.which', return_value='gh'), patch.object(module.subprocess, 'run', side_effect=AssertionError('Public installation must not invoke GitHub CLI')), patch.object(module, 'urlopen', return_value=response) as opened:
            self.assertEqual(module.github_json('repos/owner/repo/commits/main')['sha'], 'public-source')
        request = opened.call_args.args[0]
        self.assertIsNone(request.get_header('Authorization'))

    def test_api_rate_limit_does_not_ask_for_account_authorization(self):
        module = self.installer()
        error = module.HTTPError('https://api.github.com/example', 403, 'rate limit', {'X-RateLimit-Remaining': '0'}, None)
        with patch.object(module, 'urlopen', side_effect=error):
            with self.assertRaisesRegex(module.InstallError, 'GITHUB_RATE_LIMITED'):
                module.github_json('repos/owner/repo/commits/main')

    @unittest.skipUnless(os.name == 'nt', 'Windows PowerShell entrypoint')
    def test_public_entrypoint_never_executes_former_private_controller_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            appdata = home / 'AppData/Local'
            cache = appdata / 'LingxiMemoryConnector/bootstrap'
            cache.mkdir(parents=True)
            marker = appdata / 'legacy-controller-executed'
            (cache / 'agent_install.py').write_text('import os; from pathlib import Path; Path(os.environ["LOCALAPPDATA"]).joinpath("legacy-controller-executed").write_text("used"); print("{}")', encoding='utf-8')
            entrypoint = home / 'Install-Agent.ps1'
            shutil.copyfile(ROOT / 'Install-Agent.ps1', entrypoint)
            ps_path = str(entrypoint).replace("'", "''")
            py_path = sys.executable.replace("'", "''")
            command = "function Invoke-RestMethod { throw 'TEST_NETWORK_DISABLED' }; & '" + ps_path + "' -Apply -PythonExe '" + py_path + "'"
            process = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', command], env={**os.environ, 'USERPROFILE': str(home), 'LOCALAPPDATA': str(appdata)}, capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertFalse(marker.exists(), 'Public installer must not execute legacy credential-aware controller')
            self.assertNotEqual(process.returncode, 0, 'Blocked public download must be reported, never replaced by old controller')

    def test_public_controller_pin_matches_canonical_published_bytes(self):
        script = (ROOT / 'Install-Agent.ps1').read_text(encoding='utf-8')
        pin = re.search(r"\$expectedControllerSha256 = '([0-9a-f]{64})'", script).group(1)
        canonical = (ROOT / 'agent_install.py').read_bytes().replace(b'\r\n', b'\n')
        self.assertEqual(pin, hashlib.sha256(canonical).hexdigest())


class AgentInstallOutputTests(unittest.TestCase):
    def installer(self):
        spec = importlib.util.spec_from_file_location('agent_install', ROOT / 'agent_install.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_run_forces_utf8_output_for_the_calling_agent(self):
        # The installer answers the calling agent with JSON; a Windows pipe would otherwise emit
        # the machine ANSI code page, so a UTF-8 reader could not decode the Chinese detail.
        module = self.installer()

        class Stream:
            def __init__(self):
                self.reconfigured = None

            def reconfigure(self, **kwargs):
                self.reconfigured = kwargs

        out, err = Stream(), Stream()
        with patch.object(sys, 'stdout', out), patch.object(sys, 'stderr', err), \
                patch.object(module, 'main', return_value=0):
            self.assertEqual(module.run([]), 0)
        self.assertEqual(out.reconfigured, {'encoding': 'utf-8'})
        self.assertEqual(err.reconfigured, {'encoding': 'utf-8'})


if __name__ == '__main__':
    unittest.main()
