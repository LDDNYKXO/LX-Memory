import asyncio
import copy
import csv
import io
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime
from aiohttp import web
from .cloud import ConnectorError
from . import collection, page_meta
from .context import (CACHE_FRESH_SECONDS, CONTEXT_CACHE_MAX_ENTRIES, CONTEXT_TOTAL_TIMEOUT_S,
                      STATE_SERVICE_UNAVAILABLE, _fit_output, build_context, merge_key, new_request_id)
from .context_audit import AuditSnapshots

# Finished results and in-flight retrievals are bounded separately: a burst of distinct keys
# otherwise starts one task each and the finished-result cap never sees them.
CONTEXT_INFLIGHT_MAX = 4
CONTEXT_INFLIGHT_WAIT_S = 5.0


def _timeout_result(request_id):
    """Hard-deadline envelope: no retrieval result was produced, so nothing may be presented."""
    value = _busy_result(request_id)
    value['unknowns'] = ['背景检索超过硬性时间上限；本轮未产出结论，请重试或缩小项目范围。']
    value['error'] = {'code': 'CONTEXT_TIMEOUT', 'message': '背景检索超时'}
    return value


def _busy_result(request_id):
    """Refusal envelope used when the in-flight budget is exhausted; never a fabricated answer."""
    return {'state': STATE_SERVICE_UNAVAILABLE, 'request_id': request_id,
            'project': {'selected': None}, 'goal': '',
            'progress': {'executed': [], 'current': [], 'pending_verification': []},
            'next_step': '', 'constraints': [], 'sources': [], 'field_sources': {},
            'unknowns': ['并发背景请求已达上限；未执行本次检索，请稍后重试。'],
            'conflicts': [], 'retrieved_at': None,
            'consumption': {'background_sources': 'returned_cloud_sources_only',
                            'filesystem_background_fallback': False,
                            'extra_search_fallback': False,
                            'latest_state_verified': False,
                            'enforcement': 'consumer_contract_not_host_sandbox'},
            'budget': {'search_attempts': 0, 'reads': 0, 'chars': 0, 'output_chars': 0,
                       'truncated': False, 'replacement_reads': 0},
            'error': {'code': 'CONTEXT_BUSY', 'message': '并发背景请求已达上限'}}
from .proposal_validation import validate_proposal
from .session import Session
from .writeback import Writeback
from .maintenance import installed_build, unknown_receipts, digest as maintenance_digest

def private_directory(directory):
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise RuntimeError('Runtime directory must not be a symlink')
    if os.name == 'nt':
        output = subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'], text=True)
        sid = next(csv.reader(io.StringIO(output.strip())))[1]
        if not sid.startswith('S-1-'):
            raise RuntimeError('Cannot determine current Windows user')
        result = subprocess.run(['icacls', str(directory), '/inheritance:r', '/grant:r', '*' + sid + ':(OI)(CI)F'], capture_output=True)
        if result.returncode:
            raise RuntimeError('Cannot protect product session directory')
    else:
        directory.chmod(0o700)


def _respect_policy_time(data, fit=True):
    """Do not serve a prior policy decision across an explicit time boundary.

    Same-round callers still share exactly one retrieval. A boundary requires a new
    request/turn to recheck; it never starts an automatic second cloud lookup.
    """
    project = data.get('project', {})
    try:
        evaluated = datetime.fromisoformat(project['policy_evaluated_at']).timestamp()
    except (KeyError, TypeError, ValueError):
        return _fit_output(data) if fit else data
    crossed = False
    now = time.time()
    for row in project.get('retrieval_decisions', []):
        for field in ('valid_from', 'valid_until', 'as_of'):
            try:
                date = datetime.fromisoformat(row[field])
                if date.tzinfo is not None and evaluated < date.timestamp() <= now:
                    crossed = True
            except (KeyError, TypeError, ValueError):
                continue
    if not crossed:
        return _fit_output(data) if fit else data
    result = copy.deepcopy(data)
    if result['state'] == 'complete':
        result['state'] = 'partial'
    result['project']['policy_recheck_required'] = True
    result['project']['selected'] = None
    result['goal'], result['next_step'] = '', ''
    result['progress'] = {'executed': [], 'current': [], 'pending_verification': []}
    result['constraints'], result['field_sources'] = [], {}
    result['_constraints_status'] = 'unknown'
    result.pop('record', None)
    for row in result['project'].get('retrieval_decisions', []):
        if row.get('decision') == 'selected':
            row.update(decision='policy_recheck_required', reason='声明时间边界已越过，旧裁决不可作为当前结论。')
    for source in result.get('sources', []):
        if not str(source.get('path', '')).startswith('core/'):
            source['body_chars'] = source.get('body_chars', len(source.get('content') or ''))
            source['content'] = ''
            source['omitted_reason'] = 'policy_time_boundary_crossed'
    result['unknowns'].append('声明时间边界已越过，需要新一轮请求核查；本轮未自动重新检索。')
    return _fit_output(result) if fit else result

class Broker:
    def __init__(self, directory):
        self.directory = directory
        self.session = Session(directory)
        self.writeback = Writeback(self.session, directory)
        self.loaded_build = installed_build(directory.resolve().parents[1])
        self.installation_tag = maintenance_digest(str(directory.resolve().parents[1]))
        self.active_operations = 0
        self.maintenance_mode = (directory/'maintenance/active.json').exists()
        self.token = secrets.token_urlsafe(32)
        self.port = None
        self.pid = os.getpid()
        self.stop = asyncio.Event()
        self.context_cache = {}
        self.audit_snapshots = AuditSnapshots()
        # In-flight retrievals are bounded separately from finished results: every distinct key
        # otherwise starts its own task, and the finished-result cap alone cannot limit them.
        self.inflight_slots = asyncio.Semaphore(CONTEXT_INFLIGHT_MAX)

    async def _fetch(self, operation, args):
        """The only operations the shared-background path may use; everything else is refused."""
        if operation == 'status':
            return await self.session.status(refresh=True)
        if operation == 'search':
            return await self.session.search(args.get('query'), args.get('limit', 5))
        if operation == 'read':
            return await self.session.read(args.get('path'), args.get('offset', 0), args.get('max_chars', 8000))
        raise ConnectorError('INVALID_INPUT')

    async def browse(self, query, limit=10, tag=''):
        return await collection.browse(self.session, query, limit, tag)

    async def export(self, query='', paths=None, limit=10, output=''):
        return await collection.export(self.session, query, paths, limit, output, self.directory)

    async def backup(self, query='', paths=None, output_dir='', keep=5):
        return await collection.backup(self.session, query, paths, output_dir, keep, self.directory)

    async def backup_verify(self, snapshot):
        return await collection.verify_backup(self.session, snapshot, self.directory)

    async def context(self, args):
        """Same-round merge: one retrieval per (epoch, request identity, normalized clues).

        Successful results stay fresh for `CACHE_FRESH_SECONDS`; a failed lookup is never cached,
        so one outage cannot be replayed as if it were a fresh answer. Every cache entry carries
        the session `epoch` in its key, so login/logout invalidates it by construction.
        """
        view = args.get('view', 'background')
        if view not in ('background', 'audit'):
            raise ConnectorError('INVALID_INPUT')
        if not hasattr(self, 'audit_snapshots'):
            self.audit_snapshots = AuditSnapshots()
        scope = (id(self.session), self.session.epoch)
        if view == 'audit':
            return self.audit_snapshots.read(args.get('snapshot_id'), scope, args.get('cursor', ''))
        if args.get('snapshot_id') or args.get('cursor'):
            raise ConnectorError('INVALID_INPUT')

        def present(data, audit=None, cached=False):
            if scope != (id(self.session), self.session.epoch):
                raise ConnectorError('ACCOUNT_CHANGED')
            # One deep copy on the normal path; a crossed policy time boundary copies again.
            # `_respect_policy_time` returns its argument as-is when no boundary is crossed, so
            # callers must not hand in the cached entry directly.
            checked = _respect_policy_time(copy.deepcopy(data), fit=False)
            ref = (audit if not checked['project'].get('policy_recheck_required') else None)
            if ref and not self.audit_snapshots.available(ref, scope):
                ref = None
            ref = ref or self.audit_snapshots.store(checked, scope)
            # Mark the client copy only after the audit snapshot is stored: `cached` is transport
            # metadata, not evidence, and must not appear as an audit section.
            if cached:
                checked['cached'] = True
            return _fit_output(checked, audit=ref)

        request = args.get('request') if isinstance(args.get('request'), str) else ''
        identity = args.get('request_id') or args.get('turn_id') or new_request_id()
        key = merge_key(scope, identity, request,
                        project=args.get('project') or '', cwd=args.get('cwd') or '',
                        remote=args.get('remote') or '')
        entry = self.context_cache.get(key)
        if entry is not None:
            if 'data' in entry and time.monotonic() - entry.get('cached_at', 0) <= CACHE_FRESH_SECONDS:
                return present(entry['data'], entry.get('audit'), cached=True)
            if 'task' in entry:
                # A concurrent identical request joins the retrieval already in flight.
                return present(await entry['task'], cached=True)
        try:
            slots = getattr(self, 'inflight_slots', None)
            if slots is None:
                # Tests construct Broker with __new__; the bound is created on first use.
                slots = asyncio.Semaphore(CONTEXT_INFLIGHT_MAX)
                self.inflight_slots = slots
            await asyncio.wait_for(slots.acquire(), CONTEXT_INFLIGHT_WAIT_S)
        except asyncio.TimeoutError:
            # Refuse rather than queue unboundedly; the caller retries with its own budget.
            return _fit_output(_busy_result(identity))
        try:
            task = asyncio.ensure_future(build_context(self._fetch, request=request,
                                                       project=args.get('project') or '',
                                                       cwd=args.get('cwd') or '',
                                                       remote=args.get('remote') or '',
                                                       request_id=identity, raw=True,
                                                       include_personal=True))
            self.context_cache[key] = {'task': task}
            try:
                # Internal budget checks are advisory; this is the hard ceiling that also covers
                # lock waits, personal loading and slow upstream reads.
                data = await asyncio.wait_for(asyncio.shield(task), CONTEXT_TOTAL_TIMEOUT_S + 5)
            except asyncio.TimeoutError:
                task.cancel()
                self.context_cache.pop(key, None)
                return _fit_output(_timeout_result(identity))
            except BaseException:
                # A lookup that ended in an unexpected failure must never stay in the cache: a later
                # identical request would otherwise await the same dead future and fail forever.
                self.context_cache.pop(key, None)
                raise
        finally:
            slots.release()
        if data.get('state') == STATE_SERVICE_UNAVAILABLE:
            self.context_cache.pop(key, None)
        else:
            self.context_cache[key] = {'data': data, 'cached_at': time.monotonic()}
        result = present(data)
        if key in self.context_cache:
            self.context_cache[key]['audit'] = result['audit']
        # Prune only after the audit reference is written back; an in-flight `task` entry is kept.
        self._prune_context_cache(keep=key)
        return result

    def _prune_context_cache(self, keep=None):
        """Bound the cache that holds unfitted evidence.

        An entry older than `CACHE_FRESH_SECONDS` can never be reused, so it is dead weight. The
        hard entry cap mirrors the audit snapshot bound: both hold the same unfitted evidence.
        In-flight `task` entries are never touched, because concurrent requests join them.
        """
        now = time.monotonic()
        for stale in [k for k, v in self.context_cache.items()
                      if k != keep and 'data' in v
                      and now - v.get('cached_at', 0) > CACHE_FRESH_SECONDS]:
            self.context_cache.pop(stale, None)
        while len([k for k, v in self.context_cache.items() if 'data' in v]) > CONTEXT_CACHE_MAX_ENTRIES:
            oldest = min((k for k, v in self.context_cache.items() if 'data' in v and k != keep),
                         key=lambda k: self.context_cache[k].get('cached_at', 0), default=None)
            if oldest is None:
                break
            self.context_cache.pop(oldest, None)

    async def writeback_status(self):
        try:
            account = await self.session.identity()
            account_verified, error = True, None
        except ConnectorError as e:
            account_verified, error = False, e.code
        enabled = account_verified and self.session.write_enabled_for(account)
        return {'account_verified':account_verified,'write_enabled':enabled,'error':error or (None if enabled else 'UPSTREAM_CREATE_UNPROVEN')}

    def descriptor(self):
        payload = {'port': self.port, 'token': self.token, 'pid': self.pid, 'version': 1, 'loaded_build':self.loaded_build,'installation_tag':self.installation_tag}
        temporary = self.directory / 'connection.tmp'
        temporary.write_text(json.dumps(payload), encoding='utf-8')
        if os.name != 'nt':
            temporary.chmod(0o600)
        temporary.replace(self.directory / 'connection.json')

    def enter_maintenance(self):
        if self.active_operations or self.writeback.lock.locked() or unknown_receipts(self.writeback.directory):
            raise ConnectorError('BUSY_RECOVERY_REQUIRED')
        if self.session.state=='CONNECTED' and not self.session.remember:
            raise ConnectorError('TEMPORARY_LOGIN_WOULD_BE_LOST')
        self.maintenance_mode=True
        return {'state':'QUIESCED','loaded_build':self.loaded_build,'installation_tag':self.installation_tag,
                'remember_login':self.session.remember,'login_state':self.session.state}

    async def handle(self, request):
        if request.headers.get('Origin') or not secrets.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + self.token):
            return web.json_response({'ok': False, 'error': {'code': 'PERMISSION_DENIED'}}, status=403)
        if request.host != '127.0.0.1:' + str(self.port):
            return web.json_response({'ok': False, 'error': {'code': 'PERMISSION_DENIED'}}, status=403)
        counted = False
        try:
            args = await request.json()
            if not isinstance(args, dict):
                raise ConnectorError('INVALID_INPUT')
            operation = request.match_info['operation']
            if operation not in ('maintenance-enter','maintenance-exit','stop'):
                if self.maintenance_mode and operation != 'status':
                    raise ConnectorError('MAINTENANCE_IN_PROGRESS')
                self.active_operations += 1; counted = True
            if operation == 'status' and not args:
                data = await self.session.status(refresh=True)
                data = {**data,'runtime':{'loaded_build':self.loaded_build,'protocol_version':1,'maintenance':self.maintenance_mode}}
            elif operation == 'maintenance-enter' and not args:
                data = self.enter_maintenance()
            elif operation == 'maintenance-exit' and not args:
                if (self.directory/'maintenance/active.json').exists():raise ConnectorError('MAINTENANCE_IN_PROGRESS')
                self.maintenance_mode=False;data={'state':'RESUMED'}
            elif operation == 'search' and set(args) <= {'query', 'limit'}:
                data = await self.session.search(args.get('query'), args.get('limit', 5))
            elif operation == 'read' and set(args) <= {'path', 'offset', 'max_chars'}:
                data = await self.session.read(args.get('path'), args.get('offset', 0), args.get('max_chars', 8000))
                if isinstance(data, dict) and data.get('source') == 'lingxi_cloud':
                    data = page_meta.annotate(data)
            elif operation == 'browse' and set(args) <= {'query', 'limit', 'tag'}:
                data = await self.browse(args.get('query', ''), args.get('limit', 10), args.get('tag', ''))
            elif operation == 'export' and set(args) <= {'query', 'paths', 'limit', 'output'}:
                data = await self.export(args.get('query', ''), args.get('paths'), args.get('limit', 10), args.get('output', ''))
            elif operation == 'backup' and set(args) <= {'query', 'paths', 'output_dir', 'keep'}:
                data = await self.backup(args.get('query', ''), args.get('paths'), args.get('output_dir', ''), args.get('keep', 5))
            elif operation == 'backup-verify' and set(args) == {'snapshot'}:
                data = await self.backup_verify(args['snapshot'])
            elif operation == 'context' and set(args) <= {'request', 'project', 'cwd', 'remote', 'request_id', 'turn_id', 'view', 'snapshot_id', 'cursor'}:
                data = await self.context(args)
            elif operation == 'write-status' and not args:
                data = await self.writeback_status()
            elif operation == 'enable-saving' and not args:
                # Installation UI obtains explicit consent; not an MCP tool.
                data = await self.session.verify_create_contract()
            elif operation == 'prepare' and {'category','title','summary','content'} <= set(args) <= {'category','title','summary','content','assessment_confirmation','target_path','sources','related','tags','attrs'}:
                data = await self.writeback.prepare(**args)
            elif operation == 'validate' and {'category','title','summary','content'} <= set(args) <= {'category','title','summary','content','path','project','cwd','remote'}:
                data = await validate_proposal(self.session, **args)
            elif operation == 'archive' and 'target_path' in args and set(args) <= {'target_path', 'superseded_by'}:
                data = await self.writeback.prepare_archive(args['target_path'], args.get('superseded_by'))
            elif operation in ('commit','receipt') and set(args) == {'proposal_id'}:
                data = await getattr(self.writeback, operation)(args['proposal_id'])
            elif operation == 'login' and set(args) <= {'remember'}:
                data = await self.session.login(args.get('remember', False))
            elif operation == 'logout' and not args:
                data = await self.session.logout()
                self.token = secrets.token_urlsafe(32)
                self.descriptor()
            elif operation == 'stop' and not args:
                data = {'state': 'STOPPING'}
                self.stop.set()
            else:
                raise ConnectorError('INVALID_INPUT')
            return web.json_response({'ok': True, 'data': data})
        except ConnectorError as e:
            return web.json_response({'ok': False, 'error': {'code': e.code, 'message': str(e)}})
        except (ValueError, TypeError):
            return web.json_response({'ok': False, 'error': {'code': 'INVALID_INPUT'}})
        except Exception:
            # Do not leak upstream URLs, headers, credentials or memory into errors.
            return web.json_response({'ok': False, 'error': {'code': 'BROKER_UNAVAILABLE'}})
        finally:
            if counted:self.active_operations -= 1

    async def run(self):
        if not self.session.remember:
            # A crashed temporary browser must never be treated as a saved login.
            await self.session.logout()
        app = web.Application(client_max_size=65536)
        app.router.add_post('/v1/{operation}', self.handle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        self.descriptor()
        try:
            await self.stop.wait()
        finally:
            await self.session.close()
            (self.directory / 'connection.json').unlink(missing_ok=True)
            await runner.cleanup()

def serve(root):
    import msvcrt
    directory = root / 'runtime' / 'session'
    private_directory(directory)
    with (directory / 'broker.lock').open('a+b') as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return  # Another process owns this product's one session.
        try:
            service=Broker(directory)
            getattr(sys,'_lx_release_startup',lambda:None)()
            asyncio.run(service.run())
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
