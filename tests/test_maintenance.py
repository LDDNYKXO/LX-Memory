"""Synthetic maintenance faults in temporary product trees; no installed hosts/cloud."""
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from lingxi_connector import maintenance as m


class Controller:
    def __init__(self):
        self.calls = []
        self.busy = False
        self.fail_build = None
    def quiesce(self):
        self.calls.append('quiesce')
        if self.busy: raise m.MaintenanceError('BUSY_RECOVERY_REQUIRED')
        return {'running': True}
    def stop(self): self.calls.append('stop')
    def start(self): self.calls.append('start')
    def resume(self): self.calls.append('resume')
    def verify(self, expected):
        self.calls.append(('verify', expected))
        if expected == self.fail_build: raise RuntimeError('private failure must not leak')
        return True


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'product'; self.root.mkdir()
        self.src = self.root / 'src/lingxi_connector'; self.src.mkdir(parents=True)
        for name in ('broker.py', 'broker_client.py', 'writeback.py'):
            (self.src / name).write_text('VALUE = "old"\n', encoding='utf-8')
        (self.root / 'run.py').write_text('pass\n', encoding='utf-8')
        (self.root / 'requirements-portable.lock').write_text('unchanged\n')
        self.python = Path(sys.executable).resolve()
        self.controller = Controller()
    def tearDown(self): self.temp.cleanup()
    def manifest(self, root, name='package.json'):
        value = dict(schema=1, product='LX Memory', files=m.snapshot(root),
                     dependency_lock=m.file_sha(root/'requirements-portable.lock'))
        path = root/name; path.write_text(json.dumps(value), encoding='utf-8')
        return path, m.file_sha(path)
    def adopt(self):
        path, sha = self.manifest(self.root)
        return m.adopt(self.root, self.python, path, sha, apply=True)
    def candidate(self):
        import shutil
        p = self.base/'candidate';p.mkdir()
        shutil.copytree(self.root/'src', p/'src')
        shutil.copy2(self.root/'run.py', p/'run.py')
        shutil.copy2(self.root/'requirements-portable.lock', p/'requirements-portable.lock')
        (p/'src/lingxi_connector/broker.py').write_text('VALUE = "new"\n')
        return self.manifest(p)
    def plan(self):
        self.adopt();p,sha=self.candidate()
        return p, sha, m.plan_upgrade(self.root,self.python,p,sha)

    def test_doctor_is_offline_no_mutations_and_no_autostart(self):
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*'))
        with patch('subprocess.Popen', side_effect=AssertionError('must not spawn')):
            report=m.doctor(self.root,self.python)
        self.assertEqual(before, sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*')))
        self.assertIn('UNMANAGED_BUILD',report['issues'])
        self.assertFalse(report['native_verified'])

    def test_legacy_adoption_needs_exact_manifest_and_explicit_apply(self):
        p,sha=self.manifest(self.root)
        preview=m.adopt(self.root,self.python,p,sha,apply=False)
        self.assertEqual(preview['state'],'preview')
        self.assertFalse((self.root/'runtime').exists())
        with self.assertRaises(m.MaintenanceError):m.adopt(self.root,self.python,p,'0'*64,apply=True)
        self.adopt()
        self.assertEqual(m.doctor(self.root,self.python)['installed_build'],m.build_id(m.snapshot(self.root)))

    def test_plan_does_not_stop_write_or_modify_receipt(self):
        p,sha,plan=self.plan()
        before=m.file_sha(m.receipt_path(self.root))
        again=m.plan_upgrade(self.root,self.python,p,sha)
        self.assertEqual(plan['plan_id'],again['plan_id'])
        self.assertEqual(m.file_sha(m.receipt_path(self.root)),before)
        self.assertEqual(self.controller.calls,[])

    def test_normal_upgrade_and_state_preservation(self):
        p,sha,plan=self.plan()
        private=self.root/'runtime/session/writeback';private.mkdir(parents=True)
        (private/'sample.receipt.json').write_text('{"state":"saved_verified","private":"NEVER_EXPORT"}')
        result=m.apply_upgrade(self.root,self.python,p,sha,plan['plan_id'],self.controller)
        self.assertEqual(result['state'],'committed')
        self.assertEqual(m.build_id(m.snapshot(self.root)),plan['after_build'])
        self.assertIn('NEVER_EXPORT',(private/'sample.receipt.json').read_text())
        self.assertNotIn('NEVER_EXPORT',json.dumps(m.doctor(self.root,self.python)))

    def test_fail_new_verification_rolls_back_once(self):
        p,sha,plan=self.plan();self.controller.fail_build=plan['after_build']
        result=m.apply_upgrade(self.root,self.python,p,sha,plan['plan_id'],self.controller)
        self.assertEqual(result['state'],'rolled_back')
        self.assertEqual(m.build_id(m.snapshot(self.root)),plan['before_build'])
        self.assertEqual(self.controller.calls.count('start'),2)
        self.assertNotIn('private failure',json.dumps(result))

    def test_candidate_tamper_and_plan_drift_stop_before_quiesce(self):
        p,sha,plan=self.plan()
        (p.parent/'src/lingxi_connector/broker.py').write_text('changed')
        with self.assertRaises(m.MaintenanceError):m.apply_upgrade(self.root,self.python,p,sha,plan['plan_id'],self.controller)
        self.assertEqual(self.controller.calls,[])

    def test_existing_external_change_is_never_overwritten(self):
        p,sha,plan=self.plan();(self.src/'broker.py').write_text('EXTERNAL')
        with self.assertRaises(m.MaintenanceError):m.apply_upgrade(self.root,self.python,p,sha,plan['plan_id'],self.controller)
        self.assertEqual((self.src/'broker.py').read_text(),'EXTERNAL')
        self.assertEqual(self.controller.calls,[])

    def test_dependency_and_python_change_are_not_supported(self):
        p,sha,plan=self.plan()
        value=json.loads(p.read_text());value['dependency_lock']='0'*64;p.write_text(json.dumps(value))
        with self.assertRaises(m.MaintenanceError):m.plan_upgrade(self.root,self.python,p,m.file_sha(p))
        with self.assertRaises(m.MaintenanceError):m.plan_upgrade(self.root,Path('different-python').resolve(),p,sha)

    def test_target_path_escape_and_state_paths_rejected(self):
        p,sha,plan=self.plan()
        for bad in ('../outside.py','runtime/session/remember.json','requirements-portable.lock','src/lingxi_connector/../../x.py'):
            v=json.loads(p.read_text());v['files'][bad]='0'*64;p.write_text(json.dumps(v))
            with self.assertRaises(m.MaintenanceError):m.plan_upgrade(self.root,self.python,p,m.file_sha(p))

    def test_busy_writeback_blocks_upgrade(self):
        p,sha,plan=self.plan();self.controller.busy=True
        result=m.apply_upgrade(self.root,self.python,p,sha,plan['plan_id'],self.controller)
        self.assertEqual(result['state'],'blocked')
        self.assertNotIn('stop',self.controller.calls)
        self.assertEqual(m.build_id(m.snapshot(self.root)),plan['before_build'])

    def test_power_cut_then_recover_is_idempotent(self):
        p,sha,plan=self.plan()
        def cut(phase):
            if phase=='file_written':raise SystemExit('power cut')
        with self.assertRaises(SystemExit):m.apply_upgrade(self.root,self.python,p,sha,plan['plan_id'],self.controller,fault=cut)
        report=m.doctor(self.root,self.python)
        self.assertIn('INCOMPLETE_TRANSACTION',report['issues'])
        transaction=report['transactions'][0]['transaction_id']
        result=m.recover(self.root,self.python,transaction,self.controller)
        self.assertEqual(result['state'],'rolled_back')
        self.assertEqual(m.recover(self.root,self.python,transaction,self.controller)['state'],'rolled_back')
        self.assertEqual(m.build_id(m.snapshot(self.root)),plan['before_build'])

    def test_external_drift_during_failure_blocks_recovery(self):
        p,sha,plan=self.plan()
        def corrupt(phase):
            if phase=='file_written':
                (self.src/'broker.py').write_text('EXTERNAL')
                raise RuntimeError('fault')
        result=m.apply_upgrade(self.root,self.python,p,sha,plan['plan_id'],self.controller,fault=corrupt)
        self.assertEqual(result['state'],'recovery_blocked')
        self.assertEqual((self.src/'broker.py').read_text(),'EXTERNAL')

    def test_backup_corruption_stops_before_any_restore(self):
        p,sha,plan=self.plan()
        def cut(phase):
            if phase=='file_written':raise SystemExit()
        with self.assertRaises(SystemExit):m.apply_upgrade(self.root,self.python,p,sha,plan['plan_id'],self.controller,fault=cut)
        tx=m.doctor(self.root,self.python)['transactions'][0]['transaction_id']
        folder=m.maintenance_dir(self.root)/'transactions'/tx
        (folder/'0.bin').write_bytes(b'broken')
        before=m.snapshot(self.root)
        result=m.recover(self.root,self.python,tx,self.controller)
        self.assertEqual(result['state'],'recovery_blocked')
        self.assertEqual(before,m.snapshot(self.root))

    def test_concurrent_upgrade_lock_is_exclusive(self):
        self.adopt()
        with m.install_lock(self.root):
            with self.assertRaises(m.MaintenanceError):
                with m.install_lock(self.root):pass

    def test_tools_contract_accepts_seven_and_compatible_extras(self):
        tools=[{'name':name,'input_schema':{'type':'object','properties':{k:{'type':'string'} for k in fields}}} for name,fields in m.TOOL_FIELDS.items()]
        self.assertTrue(m.check_tools(tools)['compatible'])
        self.assertTrue(m.check_tools(tools+[{'name':'future'}])['compatible'])
        self.assertFalse(m.check_tools(tools[:-1])['compatible'])
        tools[0]['input_schema']['properties']={'incorrect':{}}
        # status has no required parameters, so alter prepare instead.
        next(t for t in tools if t['name']=='lingxi_memory_prepare')['input_schema']['properties']={}
        self.assertFalse(m.check_tools(tools)['compatible'])

    def test_readiness_never_claims_native_or_secret_fields(self):
        self.adopt();session=self.root/'runtime/session';session.mkdir(exist_ok=True,parents=True)
        (session/'connection.json').write_text(json.dumps({'pid':12,'port':1234,'token':'SECRET_TOKEN','version':1,'loaded_build':'older'}))
        report=m.doctor(self.root,self.python)
        self.assertIn('RUNNING_BUILD_DIFFERS',report['issues'])
        self.assertNotIn('SECRET_TOKEN',json.dumps(report))
        self.assertFalse(report['native_verified'])

    def test_no_old_receipt_is_silently_replaced(self):
        self.adopt();p,sha=self.manifest(self.root)
        with self.assertRaises(m.MaintenanceError):m.adopt(self.root,self.python,p,sha,apply=True)

    def test_no_change_does_not_restart(self):
        self.adopt()
        p=self.base/'candidate';p.mkdir()
        import shutil
        shutil.copytree(self.root/'src',p/'src');shutil.copy2(self.root/'run.py',p/'run.py')
        shutil.copy2(self.root/'requirements-portable.lock',p/'requirements-portable.lock')
        path,sha=self.manifest(p);plan=m.plan_upgrade(self.root,self.python,path,sha)
        self.assertEqual(m.apply_upgrade(self.root,self.python,path,sha,plan['plan_id'],self.controller)['state'],'no_change')
        self.assertEqual(self.controller.calls,[])

    def test_damaged_backup_symlink_or_receipt_not_overwritten(self):
        self.adopt();m.receipt_path(self.root).write_text('{broken')
        before=m.receipt_path(self.root).read_bytes()
        report=m.doctor(self.root,self.python)
        self.assertIn('UNMANAGED_BUILD',report['issues'])
        self.assertEqual(m.receipt_path(self.root).read_bytes(),before)

    def test_registration_identity_and_doctor_entry_without_dependencies(self):
        home=self.base/'home';home.mkdir();reg=home/'.lingxi-memory-connector';reg.mkdir()
        (reg/'install.json').write_text(json.dumps(dict(product='lingxi-memory-connector',root=str(self.root),python=str(self.python))))
        root,python=m.registered_install(home)
        self.assertEqual(root,self.root)
        self.assertEqual(python,self.python)
        with patch('subprocess.Popen',side_effect=AssertionError()):
            self.assertIn('UNMANAGED_BUILD',m.entry(home,mode='doctor')['issues'])

    def test_launcher_change_is_rejected_before_service_access(self):
        p,sha,plan=self.plan();(p.parent/'run.py').write_text('print("changed")')
        p,sha=self.manifest(p.parent)
        with self.assertRaises(m.MaintenanceError):m.plan_upgrade(self.root,self.python,p,sha)

    def test_incomplete_transaction_blocks_a_second_update(self):
        p,sha,plan=self.plan()
        folder=m.maintenance_dir(self.root)/'transactions'/('a'*32);folder.mkdir(parents=True)
        (folder/'journal.json').write_text('{"state":"prepared"}')
        with self.assertRaises(m.MaintenanceError):m.apply_upgrade(self.root,self.python,p,sha,plan['plan_id'],self.controller)
        self.assertEqual(self.controller.calls,[])

    def test_new_files_and_removed_files_rollback_to_exact_set(self):
        p,sha,plan=self.plan()
        (p.parent/'src/lingxi_connector/new.py').write_text('PASS = True')
        (p.parent/'src/lingxi_connector/writeback.py').unlink()
        p,sha=self.manifest(p.parent);plan=m.plan_upgrade(self.root,self.python,p,sha)
        self.controller.fail_build=plan['after_build']
        result=m.apply_upgrade(self.root,self.python,p,sha,plan['plan_id'],self.controller)
        self.assertEqual(result['state'],'rolled_back')
        self.assertFalse((self.src/'new.py').exists())
        self.assertTrue((self.src/'writeback.py').exists())

    def test_corrupt_journal_reports_blocked_without_restoring(self):
        self.adopt();folder=m.maintenance_dir(self.root)/'transactions'/('a'*32);folder.mkdir(parents=True)
        (folder/'journal.json').write_text('{broken')
        before=m.snapshot(self.root)
        self.assertEqual(m.recover(self.root,self.python,'a'*32,self.controller)['state'],'recovery_blocked')
        self.assertEqual(before,m.snapshot(self.root))

    def test_post_rollback_drift_is_not_reported_as_verified(self):
        p,sha,plan=self.plan();self.controller.fail_build=plan['after_build']
        result=m.apply_upgrade(self.root,self.python,p,sha,plan['plan_id'],self.controller)
        (self.src/'broker.py').write_text('EXTERNAL')
        self.assertEqual(m.recover(self.root,self.python,result['transaction_id'],self.controller)['state'],'recovery_blocked')

    def test_contract_rejects_unknown_new_required_parameter(self):
        tools=[{'name':n,'input_schema':{'type':'object','properties':{k:{'type':'string'} for k in fields},'required':list(fields)}} for n,fields in m.TOOL_FIELDS.items()]
        tools[0]['input_schema']['properties']['new_required']={}
        tools[0]['input_schema']['required']=['new_required']
        self.assertFalse(m.check_tools(tools)['compatible'])

    def test_wrong_service_owner_is_rejected_before_transport(self):
        session=self.root/'runtime/session';session.mkdir(parents=True)
        (session/'connection.json').write_text(json.dumps({'installation_tag':'wrong','port':1234,'token':'secret'}))
        controller=m.ServiceController(self.root,self.python)
        with self.assertRaises(m.MaintenanceError) as error:controller._call('maintenance-enter')
        self.assertEqual(error.exception.code,'SERVICE_OWNER_MISMATCH')

    def test_python_binary_drift_is_not_an_upgrade(self):
        # A private stand-in, never modifies the actual interpreter.
        python=self.base/'python.exe';python.write_bytes(b'original')
        path,sha=self.manifest(self.root)
        m.adopt(self.root,python,path,sha,apply=True)
        candidate,expected=self.candidate();python.write_bytes(b'external replacement')
        with self.assertRaises(m.MaintenanceError):m.plan_upgrade(self.root,python,candidate,expected)

    def test_launcher_blocks_before_tmp_creation(self):
        import shutil,subprocess
        source=Path(__file__).resolve().parents[1]/'run.py'
        shutil.copy2(source,self.root/'run.py')
        active=self.root/'runtime/session/maintenance/active.json';active.parent.mkdir(parents=True)
        active.write_text('{"phase":"mutating"}')
        result=subprocess.run([sys.executable,'-B',str(self.root/'run.py'),'mcp'],capture_output=True)
        self.assertEqual(result.returncode,4)
        self.assertFalse((self.root/'runtime/tmp').exists())

    def test_doctor_agent_entry_does_not_bootstrap_or_download(self):
        import importlib.util,io
        from contextlib import redirect_stdout
        source=Path(__file__).resolve().parents[1]/'agent_install.py'
        spec=importlib.util.spec_from_file_location('agent_doctor_test',source)
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        home=self.base/'home';registry=home/'.lingxi-memory-connector/install.json';registry.parent.mkdir(parents=True)
        registry.write_text(json.dumps(dict(product='lingxi-memory-connector',root=str(self.root),python=str(self.python))))
        with patch.object(Path,'home',return_value=home),patch.object(mod.runpy,'run_path',side_effect=AssertionError('bootstrap')),patch.object(mod,'download_sources',side_effect=AssertionError('download')),redirect_stdout(io.StringIO()) as output:
            mod.main(['--doctor'])
        self.assertFalse((self.root/'runtime').exists())
        self.assertIn('UNMANAGED_BUILD',json.loads(output.getvalue())['issues'])

    def test_probe_flag_cannot_fall_through_to_installation(self):
        import importlib.util,io
        from contextlib import redirect_stderr
        source=Path(__file__).resolve().parents[1]/'agent_install.py'
        spec=importlib.util.spec_from_file_location('agent_probe_flag_test',source)
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        with patch.object(mod,'existing_install',side_effect=AssertionError('must not enter install flow')),redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):mod.main(['--probe','online'])

    def test_corrupt_prior_transaction_or_active_gate_blocks_upgrade(self):
        p,sha,plan=self.plan()
        folder=m.maintenance_dir(self.root)/'transactions'/('b'*32);folder.mkdir(parents=True)
        (folder/'journal.json').write_text('{broken')
        active=m.maintenance_dir(self.root)/'active.json';active.write_text('{"phase":"mutating","transaction_id":"old"}')
        before=active.read_bytes()
        with self.assertRaises(m.MaintenanceError):m.apply_upgrade(self.root,self.python,p,sha,plan['plan_id'],self.controller)
        self.assertEqual(active.read_bytes(),before)
        self.assertEqual(self.controller.calls,[])

    def test_offline_upgrade_still_starts_validates_and_restores_stopped_state(self):
        p,sha,plan=self.plan()
        self.controller.quiesce=lambda:{'running':False}
        result=m.apply_upgrade(self.root,self.python,p,sha,plan['plan_id'],self.controller)
        self.assertEqual(result['state'],'committed')
        self.assertIn(('verify',plan['after_build']),self.controller.calls)
        self.assertIn('start',self.controller.calls)
        self.assertIn('stop',self.controller.calls)

    def test_unchanged_managed_drift_prevents_any_restore(self):
        p,sha,plan=self.plan()
        def cut(phase):
            if phase=='file_written':raise SystemExit()
        with self.assertRaises(SystemExit):m.apply_upgrade(self.root,self.python,p,sha,plan['plan_id'],self.controller,fault=cut)
        tx=m.doctor(self.root,self.python)['transactions'][0]['transaction_id']
        (self.src/'writeback.py').write_text('EXTERNAL_UNCHANGED')
        before=m.snapshot(self.root)
        self.assertEqual(m.recover(self.root,self.python,tx,self.controller)['state'],'recovery_blocked')
        self.assertEqual(before,m.snapshot(self.root))

    def test_terminal_recovery_finishes_gate_cleanup(self):
        p,sha,plan=self.plan();self.controller.fail_build=plan['after_build']
        result=m.apply_upgrade(self.root,self.python,p,sha,plan['plan_id'],self.controller)
        tx=result['transaction_id'];active=m.maintenance_dir(self.root)/'active.json'
        active.write_text(json.dumps({'phase':'ready','transaction_id':tx}))
        self.controller.calls=[]
        self.assertEqual(m.recover(self.root,self.python,tx,self.controller)['state'],'rolled_back')
        self.assertFalse(active.exists())
        self.assertIn('resume',self.controller.calls)

    def test_terminal_recovery_resumes_after_gate_was_already_removed(self):
        p,sha,plan=self.plan();self.controller.fail_build=plan['after_build']
        result=m.apply_upgrade(self.root,self.python,p,sha,plan['plan_id'],self.controller)
        self.controller.calls=[]
        self.assertEqual(m.recover(self.root,self.python,result['transaction_id'],self.controller)['state'],'rolled_back')
        self.assertIn('resume',self.controller.calls)

    def test_cold_start_status_and_hook_release_import_barrier(self):
        import shutil,subprocess
        source=Path(__file__).resolve().parents[1]
        shutil.copy2(source/'run.py',self.root/'run.py')
        shutil.copy2(source/'src/lingxi_connector/cli.py',self.src/'cli.py')
        (self.src/'__init__.py').write_text('')
        (self.src/'cloud.py').write_text('class ConnectorError(Exception): pass\n')
        (self.src/'broker.py').write_text("def serve(root):\n (root/'started.marker').write_text('started')\n")
        (self.src/'broker_client.py').write_text("import subprocess,sys\ndef call(root,*args,**kwargs):\n r=subprocess.run([sys.executable,'-B',str(root/'run.py'),'serve'],capture_output=True,timeout=5)\n assert r.returncode==0,r.stderr\n return {'state':'CONNECTED'}\n")
        (self.src/'lifecycle.py').write_text("from .broker_client import call\ndef handle_event(root,event): return call(root,'status')\n")
        for command in ('status','hook'):
            with self.subTest(command=command):
                (self.root/'started.marker').unlink(missing_ok=True)
                result=subprocess.run([sys.executable,'-B',str(self.root/'run.py'),command],input=b'{}',capture_output=True,timeout=8)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertTrue((self.root/'started.marker').exists())

    def test_contract_rejects_parameter_type_mismatch(self):
        tools=[{'name':n,'input_schema':{'type':'object','properties':{k:{'type':'integer'} for k in fields}}} for n,fields in m.TOOL_FIELDS.items()]
        self.assertFalse(m.check_tools(tools)['compatible'])

    def test_launcher_and_updater_share_import_barrier(self):
        import shutil,subprocess
        shutil.copy2(Path(__file__).resolve().parents[1]/'run.py',self.root/'run.py')
        with m.startup_lock(self.root,timeout=0):
            result=subprocess.run([sys.executable,'-B',str(self.root/'run.py'),'mcp'],capture_output=True)
        self.assertEqual(result.returncode,4)
        self.assertFalse((self.root/'runtime/tmp').exists())

    def test_recovery_does_not_replace_another_transactions_gate(self):
        p,sha,plan=self.plan()
        def cut(phase):
            if phase=='file_written':raise SystemExit()
        with self.assertRaises(SystemExit):m.apply_upgrade(self.root,self.python,p,sha,plan['plan_id'],self.controller,fault=cut)
        tx=m.doctor(self.root,self.python)['transactions'][0]['transaction_id']
        active=m.maintenance_dir(self.root)/'active.json';active.write_text('{"transaction_id":"other","phase":"mutating"}')
        before=active.read_bytes()
        self.assertEqual(m.recover(self.root,self.python,tx,self.controller)['state'],'recovery_blocked')
        self.assertEqual(active.read_bytes(),before)

    def test_missing_edge_is_reported_without_installation(self):
        with patch.object(m,'edge_available',return_value=False,create=True):report=m.doctor(self.root,self.python)
        self.assertIn('EDGE_MISSING',report['issues'])


class BrokerMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_gate_rejects_active_or_unknown_writes_then_closes_new_work(self):
        import asyncio
        from lingxi_connector.broker import Broker
        from lingxi_connector.cloud import ConnectorError
        with tempfile.TemporaryDirectory() as folder:
            broker=Broker(Path(folder))
            broker.active_operations=1
            with self.assertRaises(ConnectorError):broker.enter_maintenance()
            broker.active_operations=0
            broker.writeback.directory.mkdir()
            (broker.writeback.directory/'a.receipt.json').write_text('{"state":"unknown"}')
            with self.assertRaises(ConnectorError):broker.enter_maintenance()
            (broker.writeback.directory/'a.receipt.json').unlink()
            await broker.writeback.lock.acquire()
            with self.assertRaises(ConnectorError):broker.enter_maintenance()
            broker.writeback.lock.release()
            self.assertEqual(broker.enter_maintenance()['state'],'QUIESCED')
            self.assertTrue(broker.maintenance_mode)

    async def test_frozen_build_does_not_follow_disk_changes(self):
        from lingxi_connector.broker import Broker
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);src=root/'src/lingxi_connector';src.mkdir(parents=True)
            for name in ('broker.py','broker_client.py'):(src/name).write_text('pass')
            (root/'run.py').write_text('pass')
            broker=Broker(root/'runtime/session')
            before=broker.loaded_build
            self.assertIsNotNone(before)
            (src/'broker.py').write_text('different')
            self.assertEqual(broker.loaded_build,before)
            self.assertNotEqual(broker.loaded_build,m.installed_build(root))
