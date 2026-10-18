"""Local, explicitly approved maintenance; stdlib-only offline diagnostics.

Hashes bind an operator-reviewed candidate, not a publisher identity. Session
state is never part of a package or a rollback. No network download is provided.
"""
import ast
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import time
import uuid

TOOL_FIELDS = {'lingxi_memory_status': (), 'lingxi_memory_search': ('query',),
    'lingxi_memory_read': ('path',), 'lingxi_memory_prepare': ('category','title','summary','content'),
    'lingxi_memory_commit': ('proposal_id',), 'lingxi_memory_receipt': ('proposal_id',),
    'lingxi_memory_context': ('request',)}
REQUIRED = {'run.py','src/lingxi_connector/broker.py','src/lingxi_connector/broker_client.py'}


class MaintenanceError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(ok, code):
    if not ok:
        raise MaintenanceError(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode('utf-8')).hexdigest()


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(65536), b''):
            h.update(block)
    return h.hexdigest()


def checked_path(root, name, managed=True):
    root = Path(root).resolve()
    require(isinstance(name, str) and len(name) <= 512 and not any(c in name for c in '\\:%?#\x00'), 'INVALID_PATH')
    parts = PurePosixPath(name)
    require(not parts.is_absolute() and all(p not in ('', '.', '..') for p in name.split('/')), 'INVALID_PATH')
    if managed:
        require(name == 'run.py' or (len(parts.parts) == 3 and parts.parts[:2] == ('src','lingxi_connector') and parts.suffix == '.py')
            or (len(parts.parts) == 2 and parts.parts[0] == 'docs' and parts.suffix == '.md'), 'UNSUPPORTED_WRITE_SET')
    path = root.joinpath(*parts.parts)
    require(root in path.resolve().parents, 'PATH_ESCAPE')
    for item in (path, *path.parents):
        if item == root:
            break
        require(not item.is_symlink() and not getattr(item, 'is_junction', lambda: False)(), 'LINKED_PATH')
    return path


def load_json(path):
    path = Path(path)
    require(not path.is_symlink() and path.stat().st_size <= 2_000_000, 'INVALID_METADATA')
    def unique(pairs):
        value = {}
        for k, v in pairs:
            require(k not in value, 'DUPLICATE_METADATA_KEY')
            value[k] = v
        return value
    value = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique)
    require(isinstance(value, dict), 'INVALID_METADATA')
    return value


def atomic_json(path, value):
    path = Path(path)
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2).encode('utf-8')
    atomic_bytes(path, raw)


def atomic_bytes(path, raw):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def snapshot(root):
    root = Path(root).resolve()
    paths = [root/'run.py', *(root/'src/lingxi_connector').rglob('*.py'), *(root/'docs').glob('*.md')]
    result = {}
    for path in sorted(paths):
        if not path.exists():
            continue
        name = path.relative_to(root).as_posix()
        target = checked_path(root, name)
        require(target.is_file() and target.stat().st_size <= 2_000_000, 'INVALID_CODE_FILE')
        result[name] = file_sha(target)
    require(len(result) <= 10000, 'PACKAGE_TOO_LARGE')
    require(len({n.casefold() for n in result}) == len(result), 'CASE_COLLISION')
    return result


def build_id(files):
    return digest({k: v for k, v in files.items() if k == 'run.py' or k.endswith('.py')})


def installed_build(root):
    try:
        files = snapshot(root)
        return build_id(files) if REQUIRED <= files.keys() else None
    except (OSError, ValueError, MaintenanceError):
        return None


def maintenance_dir(root):
    return checked_path(root, 'runtime/session/maintenance', managed=False)


def receipt_path(root):
    return maintenance_dir(root)/'install.json'


@contextlib.contextmanager
def install_lock(root):
    directory = maintenance_dir(root)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = checked_path(root, 'runtime/session/maintenance/lock', managed=False)
    with path.open('a+b') as stream:
        try:
            if os.name == 'nt':
                import msvcrt
                if path.stat().st_size == 0:
                    stream.write(b'0'); stream.flush()
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise MaintenanceError('MAINTENANCE_BUSY') from None
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def startup_lock(root, timeout=5):
    """Shared with the stable launcher across all critical product imports."""
    directory=maintenance_dir(root);directory.mkdir(parents=True,exist_ok=True)
    path=checked_path(root,'runtime/session/maintenance/startup.lock',managed=False)
    with path.open('a+b') as stream:
        deadline=time.monotonic()+timeout
        while True:
            try:
                if os.name=='nt':
                    import msvcrt
                    if path.stat().st_size==0:stream.write(b'0');stream.flush()
                    stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic()>=deadline:raise MaintenanceError('STARTUP_BUSY') from None
                time.sleep(.1)
        try:yield
        finally:
            if os.name=='nt':
                stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


def read_manifest(path, expected):
    require(isinstance(expected, str) and re.fullmatch('[0-9a-f]{64}', expected), 'EXPECTED_HASH_REQUIRED')
    require(file_sha(path) == expected, 'MANIFEST_HASH_MISMATCH')
    value = load_json(path)
    files = value.get('files')
    require(value.get('schema') == 1 and value.get('product') == 'LX Memory' and isinstance(files, dict)
        and REQUIRED <= files.keys() and 1 <= len(files) <= 10000, 'INVALID_MANIFEST')
    require(len({n.casefold() for n in files}) == len(files), 'CASE_COLLISION')
    for name, sha in files.items():
        checked_path(Path(path).parent, name)
        require(isinstance(sha, str) and re.fullmatch('[0-9a-f]{64}', sha), 'INVALID_MANIFEST')
    require(snapshot(Path(path).parent) == files, 'PACKAGE_HASH_MISMATCH')
    return value


def receipt(root, python):
    value = load_json(receipt_path(root))
    require(value.get('schema') == 1 and value.get('product') == 'LX Memory'
        and value.get('root') == str(Path(root).resolve()) and value.get('python') == str(Path(python).resolve()), 'INSTALL_IDENTITY_MISMATCH')
    require(value.get('build') == build_id(value['files']), 'RECEIPT_INVALID')
    return value


def adopt(root, python, manifest, expected, apply=False):
    root, python = Path(root).resolve(), Path(python).resolve()
    require(Path(manifest).parent.resolve() == root, 'BASELINE_LOCATION_MISMATCH')
    value = read_manifest(manifest, expected)
    require(python.is_file(), 'PYTHON_MISSING')
    require(value.get('dependency_lock') == file_sha(root/'requirements-portable.lock'), 'DEPENDENCY_CHANGE_UNSUPPORTED')
    result = dict(schema=1, product='LX Memory', root=str(root), python=str(python),
        files=value['files'], build=build_id(value['files']), dependency_lock=value['dependency_lock'],
        reviewed_manifest=expected,python_sha256=file_sha(python))
    require(not receipt_path(root).exists(), 'BASELINE_ALREADY_EXISTS')
    if apply:
        with install_lock(root):
            require(not receipt_path(root).exists(), 'BASELINE_ALREADY_EXISTS')
            require(snapshot(root) == result['files'], 'LOCAL_DRIFT')
            atomic_json(receipt_path(root), result)
    return {'state': 'adopted' if apply else 'preview', 'receipt': result}


def plan_upgrade(root, python, manifest, expected):
    root, python, manifest = Path(root).resolve(), Path(python).resolve(), Path(manifest).resolve()
    require(not manifest.is_relative_to(root), 'CANDIDATE_INSIDE_INSTALL')
    old = receipt(root, python)
    require(file_sha(python)==old.get('python_sha256'),'PYTHON_DRIFT')
    require(snapshot(root) == old['files'], 'LOCAL_DRIFT')
    require(file_sha(root/'requirements-portable.lock') == old['dependency_lock'], 'DEPENDENCY_DRIFT')
    new = read_manifest(manifest, expected)
    require(new['files']['run.py'] == old['files']['run.py'], 'LAUNCHER_CHANGE_UNSUPPORTED')
    require(new.get('dependency_lock') == old['dependency_lock'] and not new.get('migration_required'), 'DEPENDENCY_CHANGE_UNSUPPORTED')
    for name in new['files']:
        if name.endswith('.py'):
            try:
                ast.parse(checked_path(manifest.parent, name).read_text(encoding='utf-8-sig'))
            except (SyntaxError, UnicodeError):
                raise MaintenanceError('CANDIDATE_SYNTAX_ERROR') from None
    entries = [{'path': n, 'before': old['files'].get(n), 'after': new['files'].get(n)}
        for n in sorted(old['files'].keys() | new['files'].keys()) if old['files'].get(n) != new['files'].get(n)]
    after = {**old, 'files': new['files'], 'build': build_id(new['files']), 'reviewed_manifest': expected}
    value = dict(before_build=old['build'], after_build=after['build'], before_receipt=old,
        after_receipt=after, entries=entries, candidate=str(manifest), manifest_sha256=expected)
    return {**value, 'plan_id': digest(value)}


def _transaction(root, identifier):
    require(isinstance(identifier, str) and re.fullmatch('[0-9a-f]{32}', identifier), 'INVALID_TRANSACTION')
    return checked_path(root, 'runtime/session/maintenance/transactions/'+identifier, managed=False)


def _recover_locked(root, python, identifier, controller):
    folder = _transaction(root, identifier)
    try:
        journal = load_json(folder/'journal.json')
    except (OSError,ValueError,MaintenanceError):
        return {'state':'recovery_blocked','transaction_id':identifier,'error':'INVALID_TRANSACTION'}
    try:
        require(journal.get('schema') == 1 and journal.get('root') == str(Path(root).resolve())
            and journal.get('python') == str(Path(python).resolve()), 'INVALID_TRANSACTION')
        plan = journal['plan']
        require(digest({k: v for k,v in plan.items() if k != 'plan_id'}) == plan['plan_id'], 'JOURNAL_HASH_MISMATCH')
        require(receipt(root, python) in (plan['before_receipt'], plan['after_receipt']), 'RECEIPT_DRIFT')
        require(file_sha(Path(root)/'requirements-portable.lock')==plan['before_receipt']['dependency_lock'], 'DEPENDENCY_DRIFT')
        active=maintenance_dir(root)/'active.json'
        if active.exists():require(load_json(active).get('transaction_id')==identifier,'RECOVERY_OWNER_MISMATCH')
        if journal['state']=='rolled_back':
            require(snapshot(root)==plan['before_receipt']['files'],'RECOVERY_DRIFT')
            active=maintenance_dir(root)/'active.json'
            if active.exists():
                require(load_json(active).get('transaction_id')==identifier,'RECOVERY_OWNER_MISMATCH')
                active.unlink()
            controller.resume()
            return {'state':'rolled_back','transaction_id':identifier}
        current=snapshot(root)
        before,after=plan['before_receipt']['files'],plan['after_receipt']['files']
        require(not(current.keys()-(before.keys()|after.keys())),'RECOVERY_DRIFT')
        for name in before.keys()|after.keys():
            require(current.get(name) in (before.get(name),after.get(name)),'RECOVERY_DRIFT')
        # Validate every target and backup before stopping or restoring anything.
        for index, row in enumerate(plan['entries']):
            path = checked_path(root, row['path'])
            actual = file_sha(path) if path.exists() else None
            require(actual in (row['before'],row['after']), 'RECOVERY_DRIFT')
            if row['before'] is not None:
                backup = folder/f'{index}.bin'
                require(not backup.is_symlink() and file_sha(backup) == row['before'], 'BACKUP_CORRUPT')
        atomic_json(maintenance_dir(root)/'active.json', {'transaction_id':identifier,'phase':'mutating'})
        info = controller.quiesce()
        if info.get('running'):
            controller.stop()
        with startup_lock(root):
            journal['state'] = 'rolling_back'; atomic_json(folder/'journal.json', journal)
            for index,row in enumerate(plan['entries']):
                path = checked_path(root,row['path'])
                require((file_sha(path) if path.exists() else None) in (row['before'],row['after']), 'RECOVERY_DRIFT')
                if row['before'] is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_bytes(path,(folder/f'{index}.bin').read_bytes())
            require(snapshot(root) == plan['before_receipt']['files'], 'RESTORE_MISMATCH')
            atomic_json(receipt_path(root), plan['before_receipt'])
            atomic_json(maintenance_dir(root)/'active.json', {'transaction_id':identifier,'phase':'ready'})
        controller.start();require(controller.verify(plan['before_build']) is True, 'RESTORE_VERIFY_FAILED')
        if not journal.get('was_running', info.get('running')):controller.stop()
        journal['state'] = 'rolled_back'; atomic_json(folder/'journal.json', journal)
        (maintenance_dir(root)/'active.json').unlink(missing_ok=True)
        controller.resume()
        return {'state': 'rolled_back', 'transaction_id': identifier}
    except Exception:
        journal['state'] = 'recovery_blocked'; atomic_json(folder/'journal.json', journal)
        return {'state': 'recovery_blocked', 'transaction_id': identifier, 'error': 'MANUAL_REVIEW_REQUIRED'}


def recover(root, python, identifier, controller):
    with install_lock(root):
        return _recover_locked(root, python, identifier, controller)


def apply_upgrade(root, python, manifest, expected, plan_id, controller, fault=None):
    with install_lock(root):
        plan = plan_upgrade(root,python,manifest,expected)
        require(plan['plan_id'] == plan_id, 'PLAN_CHANGED')
        report=doctor(root,python)
        require(not ({'TRANSACTION_INVALID','INCOMPLETE_TRANSACTION'} & set(report['issues']))
            and not (maintenance_dir(root)/'active.json').exists(), 'INCOMPLETE_TRANSACTION')
        if not plan['entries']:
            return {'state':'no_change','build':plan['before_build']}
        identifier = uuid.uuid4().hex; folder = _transaction(root,identifier);folder.mkdir(parents=True)
        for i,row in enumerate(plan['entries']):
            if row['before'] is not None:
                path = checked_path(root,row['path']); (folder/f'{i}.bin').write_bytes(path.read_bytes())
                require(file_sha(folder/f'{i}.bin') == row['before'], 'BACKUP_MISMATCH')
        journal = dict(schema=1, root=str(Path(root).resolve()), python=str(Path(python).resolve()), state='prepared', plan=plan)
        atomic_json(folder/'journal.json',journal)
        gated = False
        try:
            info = controller.quiesce();gated = True
            journal.update(state='quiesced',was_running=bool(info.get('running')));atomic_json(folder/'journal.json',journal)
            atomic_json(maintenance_dir(root)/'active.json', {'transaction_id':identifier,'phase':'mutating'})
            require(snapshot(root) == plan['before_receipt']['files'], 'LOCAL_DRIFT')
            if info.get('running'):
                controller.stop()
            with startup_lock(root):
                journal['state']='copying';atomic_json(folder/'journal.json',journal)
                for row in plan['entries']:
                    target = checked_path(root,row['path'])
                    require((file_sha(target) if target.exists() else None) == row['before'], 'LOCAL_DRIFT')
                    if row['after'] is None:
                        target.unlink()
                    else:
                        source = checked_path(Path(manifest).resolve().parent,row['path'])
                        require(file_sha(source) == row['after'], 'CANDIDATE_DRIFT')
                        target.parent.mkdir(parents=True,exist_ok=True);atomic_bytes(target,source.read_bytes())
                    if fault: fault('file_written')
                require(snapshot(root) == plan['after_receipt']['files'], 'INSTALL_MISMATCH')
                atomic_json(maintenance_dir(root)/'active.json', {'transaction_id':identifier,'phase':'ready'})
            controller.start();require(controller.verify(plan['after_build']) is True, 'NEW_VERIFY_FAILED')
            if not info.get('running'):controller.stop()
            atomic_json(receipt_path(root),plan['after_receipt'])
            journal['state']='committed';atomic_json(folder/'journal.json',journal)
            (maintenance_dir(root)/'active.json').unlink(missing_ok=True)
            controller.resume()
            return {'state':'committed','transaction_id':identifier,'build':plan['after_build'],'native_verified':False}
        except Exception:
            if not gated:
                journal['state']='blocked';atomic_json(folder/'journal.json',journal)
                return {'state':'blocked','transaction_id':identifier,'error':'BUSY_RECOVERY_REQUIRED'}
            return _recover_locked(root,python,identifier,controller)


def check_tools(tools):
    rows = {t['name']: t for t in tools if isinstance(t,dict) and isinstance(t.get('name'),str)}
    issues = []
    for name, fields in TOOL_FIELDS.items():
        schema=rows.get(name,{}).get('input_schema',{})
        props=schema.get('properties',{}) if isinstance(schema,dict) else {}
        required=schema.get('required',[]) if isinstance(schema,dict) else []
        if (name not in rows or schema.get('type')!='object' or not isinstance(props,dict) or not set(fields)<=props.keys()
                or any(not isinstance(props.get(k),dict) or props[k].get('type')!='string' for k in fields)
                or not isinstance(required,list) or not all(isinstance(k,str) for k in required)
                or not set(required)<=set(fields)):
            issues.append(name)
    return {'compatible':not issues,'missing_or_incompatible':issues}


def unknown_receipts(directory):
    for path in Path(directory).glob('*.receipt.json'):
        try:
            if load_json(path).get('state') == 'unknown':
                return True
        except (OSError, ValueError, MaintenanceError):
            return True
    return False


def edge_available():
    paths=[Path(os.environ.get('PROGRAMFILES(X86)','C:/Program Files (x86)'))/'Microsoft/Edge/Application/msedge.exe',
        Path(os.environ.get('PROGRAMFILES','C:/Program Files'))/'Microsoft/Edge/Application/msedge.exe',
        Path.home()/'AppData/Local/Microsoft/Edge/Application/msedge.exe']
    return any(p.is_file() for p in paths)


def doctor(root, python):
    """Read files only: no imports of product deps, bootstrap, writes or probes."""
    root, python = Path(root).resolve(),Path(python).resolve()
    report = dict(state='checked',issues=[],installed_build=installed_build(root),running_build=None,
        transactions=[],native_verified=False,cloud_verified=False,dependencies_import_verified=False)
    if not python.is_file():report['issues'].append('PYTHON_MISSING')
    if not edge_available():report['issues'].append('EDGE_MISSING')
    deps=root/('vendor/deps' if python == root/'vendor/python/python.exe' else 'runtime/deps')
    if not all((deps/n).exists() for n in ('mcp','aiohttp','playwright','yaml')):report['issues'].append('DEPENDENCIES_MISSING')
    try:
        r=receipt(root,python)
        if snapshot(root)!=r['files']:report['issues'].append('LOCAL_DRIFT')
        if python.is_file() and file_sha(python)!=r.get('python_sha256'):report['issues'].append('PYTHON_DRIFT')
        dep_lock=root/'requirements-portable.lock'
        if not dep_lock.exists() or file_sha(dep_lock)!=r.get('dependency_lock'):report['issues'].append('DEPENDENCY_DRIFT')
    except (OSError,ValueError,KeyError,TypeError,MaintenanceError):report['issues'].append('UNMANAGED_BUILD')
    descriptor=root/'runtime/session/connection.json'
    if descriptor.exists():
        try:
            info=load_json(descriptor);report['running_build']=info.get('loaded_build')
            report['descriptor_present']=True
            if not info.get('loaded_build'):report['issues'].append('RUNNING_BUILD_UNKNOWN')
            elif info['loaded_build']!=report['installed_build']:report['issues'].append('RUNNING_BUILD_DIFFERS')
        except (OSError,ValueError,MaintenanceError):report['issues'].append('SERVICE_DESCRIPTOR_INVALID')
    else:report['issues'].append('SERVICE_NOT_OBSERVED')
    if unknown_receipts(root/'runtime/session/writeback'):report['issues'].append('WRITE_OUTCOME_UNKNOWN')
    directory=maintenance_dir(root)/'transactions'
    for path in sorted(directory.glob('*/journal.json')):
        try:
            value=load_json(path);state=value.get('state')
            report['transactions'].append({'transaction_id':path.parent.name,'state':state})
            if state not in ('committed','rolled_back','blocked'):report['issues'].append('INCOMPLETE_TRANSACTION')
        except (OSError,ValueError,MaintenanceError):report['issues'].append('TRANSACTION_INVALID')
    report['issues']=list(dict.fromkeys(report['issues']))
    actions={'UNMANAGED_BUILD':'安装构建尚未登记；先核对并预览基线，不自动修复。',
        'PYTHON_MISSING':'登记的解释器不存在；保持原安装，确认路径。',
        'PYTHON_DRIFT':'解释器文件已变化；本轮不升级或替换解释器。',
        'EDGE_MISSING':'未找到Microsoft Edge；未自动安装或改浏览器配置。',
        'DEPENDENCIES_MISSING':'隔离依赖不完整；本诊断未安装任何依赖。',
        'LOCAL_DRIFT':'受管文件有变化；先核对来源，不覆盖。',
        'RUNNING_BUILD_DIFFERS':'磁盘代码与服务构建不同；先确认当前任务，勿冒充已热更新。',
        'RUNNING_BUILD_UNKNOWN':'旧服务未提供构建标识；运行版本未知。',
        'INCOMPLETE_TRANSACTION':'存在未完成升级；只恢复该事务，不重复安装。',
        'WRITE_OUTCOME_UNKNOWN':'保存结果未知；只核对回执，不重新提交。',
        'SERVICE_NOT_OBSERVED':'未观察到服务描述；未自动启动。'}
    report['actions']=[{'code':c,'next_action':actions.get(c,'保留现场并核对本产品元数据。')} for c in report['issues']]
    if report['issues']:report['state']='needs_attention'
    return report


def registered_install(home):
    path=Path(home)/'.lingxi-memory-connector/install.json'
    value=load_json(path)
    require(value.get('product')=='lingxi-memory-connector' and isinstance(value.get('root'),str)
        and isinstance(value.get('python'),str), 'INSTALL_IDENTITY_MISMATCH')
    root,python=Path(value['root']),Path(value['python'])
    require(root.is_absolute() and python.is_absolute() and not root.is_symlink(), 'INSTALL_IDENTITY_MISMATCH')
    return root.resolve(),python.resolve()


class ServiceController:
    """Current Windows stdio service only; never starts a host or repairs configs."""
    def __init__(self, root, python):
        self.root,self.python=Path(root).resolve(),Path(python).resolve()
        self.lease=None;self.baseline=None

    def _call(self, operation, args=None):
        import sys
        require(Path(sys.executable).resolve()==self.python, 'REGISTERED_PYTHON_REQUIRED')
        descriptor=load_json(self.root/'runtime/session/connection.json')
        require(descriptor.get('installation_tag')==digest(str(self.root)), 'SERVICE_OWNER_MISMATCH')
        sys.path.insert(0,str(self.root/'src'))
        from lingxi_connector.broker_client import call
        try:
            return call(self.root,operation,args or {},autostart=False,timeout=15)
        except Exception as e:
            raise MaintenanceError(getattr(e,'code','SERVICE_UNAVAILABLE')) from None

    def _lease(self):
        if self.lease:return
        require(os.name=='nt','UNSUPPORTED_PLATFORM')
        import msvcrt
        path=checked_path(self.root,'runtime/session/broker.lock',managed=False)
        path.parent.mkdir(parents=True,exist_ok=True)
        stream=path.open('a+b')
        try:
            if path.stat().st_size==0:stream.write(b'0');stream.flush()
            stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            self.lease=stream
        except OSError:
            stream.close();raise MaintenanceError('SERVICE_STILL_RUNNING') from None

    def close(self):
        if self.lease:
            import msvcrt
            self.lease.seek(0);msvcrt.locking(self.lease.fileno(),msvcrt.LK_UNLCK,1)
            self.lease.close();self.lease=None

    def quiesce(self):
        descriptor=self.root/'runtime/session/connection.json'
        if self.lease:return {'running':False}
        if not descriptor.exists():
            self._lease();return {'running':False}
        result=self._call('maintenance-enter')
        require(result.get('state')=='QUIESCED','SERVICE_GATE_UNAVAILABLE')
        if self.baseline is None:self.baseline=result
        return {'running':True}

    def stop(self):
        if not self.lease:
            self._call('stop');self._lease()

    def start(self):
        import subprocess,time
        self.close()
        log=checked_path(self.root,'runtime/session/maintenance/service.log',managed=False)
        with log.open('ab') as stream:
            child=subprocess.Popen([str(self.python),'-B',str(self.root/'run.py'),'serve'],cwd=str(self.root),
                stdin=subprocess.DEVNULL,stdout=stream,stderr=stream,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0,
                env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','PYTHONIOENCODING':'utf-8'})
        for _ in range(75):
            if child.poll() is not None:raise MaintenanceError('SERVICE_START_FAILED')
            path=self.root/'runtime/session/connection.json'
            if path.exists():
                try:
                    if load_json(path).get('pid')==child.pid:return
                except (OSError,ValueError,MaintenanceError):pass
            time.sleep(.2)
        child.terminate();child.wait(timeout=5)
        raise MaintenanceError('SERVICE_START_TIMEOUT')

    def resume(self):
        self.close()
        if (self.root/'runtime/session/connection.json').exists():
            self._call('maintenance-exit')

    def probe_mcp(self):
        import subprocess
        code="""import runpy,sys,asyncio,json
from pathlib import Path
r=Path(sys.argv[1]);runpy.run_path(str(r/'run.py'),run_name='maintenance_probe')
from mcp import ClientSession
from mcp.client.stdio import stdio_client,StdioServerParameters
async def probe():
 async with stdio_client(StdioServerParameters(command=sys.executable,args=['-B',str(r/'run.py'),'mcp'])) as (rd,wr):
  async with ClientSession(rd,wr) as s:
   info=await s.initialize();tools=await s.list_tools()
   print(json.dumps({'version':info.server_info.version,'tools':[{'name':t.name,'input_schema':t.input_schema} for t in tools.tools]}))
asyncio.run(probe())
"""
        result=subprocess.run([str(self.python),'-B','-c',code,str(self.root)],capture_output=True,timeout=20,
            env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','PYTHONIOENCODING':'utf-8'})
        require(result.returncode==0,'MCP_PROBE_FAILED')
        try:return json.loads(result.stdout.decode('utf-8'))
        except (ValueError,UnicodeError):raise MaintenanceError('MCP_PROBE_FAILED') from None

    def verify(self, expected):
        status=self._call('status')
        require(status.get('runtime',{}).get('loaded_build')==expected,'RUNNING_BUILD_MISMATCH')
        if self.baseline:
            require(status.get('remember_login')==self.baseline.get('remember_login'),'LOGIN_STATE_CHANGED')
            if self.baseline.get('login_state')=='CONNECTED':require(status.get('cloud_available') is True,'LOGIN_NOT_PRESERVED')
        probe=self.probe_mcp()
        require(check_tools(probe.get('tools',[]))['compatible'],'MCP_CONTRACT_INCOMPATIBLE')
        require(probe.get('version','').endswith('+'+expected[:12]),'MCP_BUILD_MISMATCH')
        return True


def entry(home, *, mode, manifest=None, expected=None, plan_id=None, transaction=None, apply=False, probe=None):
    root,python=registered_install(home)
    if mode=='doctor':
        require(not apply,'DOCTOR_IS_READ_ONLY')
        report=doctor(root,python)
        if probe:
            controller=ServiceController(root,python)
            if probe=='mcp':
                result=controller.probe_mcp();report['mcp']=check_tools(result.get('tools',[]))
            elif probe=='online':
                status=controller._call('status')
                report['cloud_verified']=status.get('cloud_available') is True
                report['login_state']=status.get('state')
            else:raise MaintenanceError('INVALID_PROBE')
        return report
    require(probe is None,'INVALID_PROBE')
    if mode=='adopt':return adopt(root,python,manifest,expected,apply=apply)
    if mode=='plan':
        require(not apply,'PLAN_IS_READ_ONLY')
        return plan_upgrade(root,python,manifest,expected)
    require(apply,'APPLY_REQUIRED')
    controller=ServiceController(root,python)
    try:
        if mode=='apply':return apply_upgrade(root,python,manifest,expected,plan_id,controller)
        if mode=='recover':return recover(root,python,transaction,controller)
        raise MaintenanceError('INVALID_MODE')
    finally:controller.close()
