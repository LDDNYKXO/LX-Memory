import asyncio
import csv
import io
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from aiohttp import web
from .cloud import ConnectorError
from .session import Session
from .writeback import Writeback

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

class Broker:
    def __init__(self, directory):
        self.directory = directory
        self.session = Session(directory)
        self.writeback = Writeback(self.session, directory)
        self.token = secrets.token_urlsafe(32)
        self.port = None
        self.pid = os.getpid()
        self.stop = asyncio.Event()

    async def writeback_status(self):
        try:
            account = await self.session.identity()
            account_verified, error = True, None
        except ConnectorError as e:
            account_verified, error = False, e.code
        enabled = account_verified and self.session.write_enabled_for(account)
        return {'account_verified':account_verified,'write_enabled':enabled,'error':error or (None if enabled else 'UPSTREAM_CREATE_UNPROVEN')}

    def descriptor(self):
        payload = {'port': self.port, 'token': self.token, 'pid': self.pid, 'version': 1}
        temporary = self.directory / 'connection.tmp'
        temporary.write_text(json.dumps(payload), encoding='utf-8')
        if os.name != 'nt':
            temporary.chmod(0o600)
        temporary.replace(self.directory / 'connection.json')

    async def handle(self, request):
        if request.headers.get('Origin') or not secrets.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + self.token):
            return web.json_response({'ok': False, 'error': {'code': 'PERMISSION_DENIED'}}, status=403)
        if request.host != '127.0.0.1:' + str(self.port):
            return web.json_response({'ok': False, 'error': {'code': 'PERMISSION_DENIED'}}, status=403)
        try:
            args = await request.json()
            if not isinstance(args, dict):
                raise ConnectorError('INVALID_INPUT')
            operation = request.match_info['operation']
            if operation == 'status' and not args:
                data = await self.session.status(refresh=True)
            elif operation == 'search' and set(args) <= {'query', 'limit'}:
                data = await self.session.search(args.get('query'), args.get('limit', 5))
            elif operation == 'read' and set(args) <= {'path', 'offset', 'max_chars'}:
                data = await self.session.read(args.get('path'), args.get('offset', 0), args.get('max_chars', 8000))
            elif operation == 'write-status' and not args:
                data = await self.writeback_status()
            elif operation == 'enable-saving' and not args:
                # Installation UI obtains explicit consent; not an MCP tool.
                data = await self.session.verify_create_contract()
            elif operation == 'prepare' and set(args) == {'category','title','summary','content'}:
                data = await self.writeback.prepare(**args)
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
            asyncio.run(Broker(directory).run())
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
