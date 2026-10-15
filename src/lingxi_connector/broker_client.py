import json
import os
import subprocess
import sys
import time
from urllib.request import Request, build_opener, ProxyHandler
from .cloud import ConnectorError

def wait_exit(pid):
    if os.name == 'nt' and type(pid) is int and pid > 0:
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x100000, False, pid)
        if handle:
            try:
                if api.WaitForSingleObject(handle, 10000) == 258:
                    raise ConnectorError('BROKER_UNAVAILABLE', '会话服务仍在退出。')
            finally:
                api.CloseHandle(handle)

def call(root, operation, args=None, autostart=True):
    descriptor = root / 'runtime' / 'session' / 'connection.json'
    started = False
    opener = build_opener(ProxyHandler({}))
    for attempt in range(50):
        request_attempted = False
        try:
            info = json.loads(descriptor.read_text(encoding='utf-8'))
            port, token = info['port'], info['token']
            if type(port) is not int or not 1024 <= port <= 65535 or not isinstance(token, str) or len(token) < 32:
                raise ValueError('Bad descriptor')
            req = Request('http://127.0.0.1:%s/v1/%s' % (port, operation),
                          data=json.dumps(args or {}).encode('utf-8'),
                          headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token}, method='POST')
            request_attempted = True
            with opener.open(req, timeout=240 if operation == 'enable-saving' else 65) as response:
                result = json.load(response)
            if not result['ok']:
                error = result['error']
                raise ConnectorError(error['code'], error.get('message', error['code']))
            if operation == 'stop':
                wait_exit(info.get('pid'))
            return result['data']
        except ConnectorError:
            raise
        except (OSError, ValueError, KeyError):
            if operation == 'enable-saving' and request_attempted:
                raise ConnectorError('WRITE_OUTCOME_UNKNOWN', '校验请求结果不明确，未自动重发。') from None
            if not autostart:
                raise ConnectorError('BROKER_UNAVAILABLE') from None
            if not started:
                (root / 'runtime').mkdir(exist_ok=True)
                # No shell, no visible console helper; the login browser itself is intentional UI.
                log = (root / 'runtime' / 'broker.log').open('ab')
                try:
                    subprocess.Popen([sys.executable, '-B', str(root / 'run.py'), 'serve'],
                                     cwd=str(root), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                                     env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONIOENCODING': 'utf-8'})
                finally:
                    log.close()
                started = True
            time.sleep(0.2)
    raise ConnectorError('BROKER_UNAVAILABLE', '本机会话服务启动失败，请检查Edge及隔离依赖。')
