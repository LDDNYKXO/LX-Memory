"""Agent entrypoint: source installation, shared local authorization, stdio MCP config.

Only standard-library imports until the existing connector has been selected.
Credentials, browser profiles and cloud data are never downloaded or copied.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import runpy
import subprocess
import sys
import tempfile
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

REPOSITORY = 'LDDNYKXO/lingxi-memory-connector'
ROOT_FILES = {'run.py', 'setup_wizard.py', 'write_config.ps1', 'requirements-portable.lock',
              'Start.ps1', 'PORTABLE-RUNTIME-SOURCE.json'}
CLIENTS = ('Codex', 'DSH', 'WorkBuddy', 'ZCode', 'Generic')


class InstallError(Exception):
    pass


def source_file(name):
    path = PurePosixPath(name)
    if '\\' in name or path.is_absolute() or '..' in path.parts:
        return False
    return name in ROOT_FILES or (
        len(path.parts) == 3 and path.parts[:2] == ('src', 'lingxi_connector') and path.suffix == '.py'
    ) or name == 'skills/lingxi-cloud-memory/SKILL.md'


def existing_install(home):
    registry = home / '.lingxi-memory-connector/install.json'
    if not registry.exists():
        return None
    try:
        if registry.is_symlink():
            raise ValueError()
        data = json.loads(registry.read_text(encoding='utf-8-sig'))
        root, python = Path(data['root']), Path(data['python'])
        if data['product'] != 'lingxi-memory-connector' or not root.is_absolute() or not python.is_absolute():
            raise ValueError()
        if not (root / 'run.py').is_file() or not (root / 'src/lingxi_connector/broker_client.py').is_file() or not python.is_file():
            raise ValueError()
        return root.resolve(), python.resolve()
    except (ValueError, KeyError, TypeError, OSError):
        raise InstallError('EXISTING_INSTALL_INVALID: 保留原登记，请核查原安装；不会另外创建登录副本。') from None


def check_connection(root, broker, login=False, remember=False):
    status = broker(root, 'status')
    state = status.get('state')
    result = {'state': state, 'action': 'check_error', 'error': status.get('error')}
    if state == 'CONNECTED' and status.get('cloud_available') is True:
        result['action'] = 'ready'
    elif state in ('NOT_LOGGED_IN', 'AUTH_REQUIRED'):
        result['action'] = 'login_required'
        if login:
            opened = broker(root, 'login', {'remember': remember})
            result.update(state=opened.get('state'), action='finish_login')
    elif state == 'LOGIN_IN_PROGRESS':
        result['action'] = 'finish_login'
    return result


def configure_clients(root, home, python, clients, apply=False):
    known = [client for client in clients if client != 'Generic']
    if known:
        from lingxi_connector.setup import configure
        result = configure(root, home, python, known, apply=apply)
    else:
        result = {'applied': apply, 'clients': []}
        if apply:
            from lingxi_connector.setup import safe_target, load_json
            registry = home / '.lingxi-memory-connector/install.json'
            safe_target(registry, home)
            prior = load_json(registry.read_bytes()) if registry.exists() else None
            value = {'product': 'lingxi-memory-connector', 'root': str(root.resolve()),
                     'roots': [str(root.resolve())], 'python': str(python.resolve())}
            if prior is None:
                registry.parent.mkdir(parents=True, exist_ok=True)
                # Exclusive creation never replaces an existing authorization route.
                with registry.open('x', encoding='utf-8') as stream:
                    json.dump(value, stream, ensure_ascii=False, indent=2)
            elif prior.get('root') != value['root'] or prior.get('python') != value['python']:
                raise InstallError('INSTALL_REGISTRY_CHANGED: 安装登记已变化，未覆盖。')
    result['mcpServers'] = {'lingxi-memory': {
        'command': str(python), 'args': ['-B', str(root / 'run.py'), 'mcp'],
        'env': {'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1'}}}
    result['skill_path'] = str(root / 'skills/lingxi-cloud-memory/SKILL.md')
    result['native_verified'] = False
    return result


def github_json(endpoint):
    """Fetch public product source anonymously; never inspect GitHub credentials."""
    try:
        request = Request('https://api.github.com/' + endpoint,
                          headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'Lingxi-Memory-Installer'})
        with urlopen(request, timeout=60) as response:
            return json.load(response)
    except HTTPError as error:
        if error.code in (403, 429) and (error.code == 429 or error.headers.get('X-RateLimit-Remaining') == '0'):
            raise InstallError('GITHUB_RATE_LIMITED: 公共下载达到限流，请稍后重试；不需要 GitHub 登录。') from None
        raise InstallError('GITHUB_DOWNLOAD_FAILED: 公共源码暂不可用，请检查网络、仓库地址或稍后重试；不需要提供账号或令牌。') from None
    except (URLError, TimeoutError):
        raise InstallError('GITHUB_NETWORK_ERROR: 下载失败，原连接器和登录保留。') from None


def download_sources(destination, ref):
    # Resolve once, then read every file at that exact tree commit.
    commit = github_json(f'repos/{REPOSITORY}/commits/{ref}')['sha']
    if len(commit) != 40 or any(c not in '0123456789abcdef' for c in commit):
        raise InstallError('INVALID_GITHUB_COMMIT')
    tree = github_json(f'repos/{REPOSITORY}/git/trees/{commit}?recursive=1')
    if tree.get('truncated'):
        raise InstallError('INCOMPLETE_GITHUB_TREE')
    files = [item for item in tree['tree'] if item.get('type') == 'blob' and source_file(item['path'])]
    names = {item['path'] for item in files}
    if not ROOT_FILES.issubset(names) or not {'src/lingxi_connector/broker.py', 'src/lingxi_connector/setup.py', 'skills/lingxi-cloud-memory/SKILL.md'}.issubset(names):
        raise InstallError('INCOMPLETE_CONNECTOR_SOURCE')
    for item in files:
        if item.get('mode') != '100644':
            raise InstallError('UNSUPPORTED_SOURCE_FILE')
        data = github_json(f"repos/{REPOSITORY}/contents/{item['path']}?ref={commit}")
        if data.get('encoding') != 'base64':
            raise InstallError('INVALID_GITHUB_CONTENT')
        raw = base64.b64decode(data['content'])
        blob = b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw
        if hashlib.sha1(blob).hexdigest() != item['sha']:
            raise InstallError('SOURCE_HASH_MISMATCH')
        target = destination / item['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    return commit


def install_dependencies(root, python):
    bundled = python.resolve() == (root / 'vendor/python/python.exe').resolve()
    deps = root / ('vendor/deps' if bundled else 'runtime/deps')
    deps.mkdir(parents=True, exist_ok=True)
    pip = root / 'runtime/pip.pyz'
    request = Request('https://bootstrap.pypa.io/pip/pip.pyz', headers={'User-Agent': 'Lingxi-Memory-Installer'})
    with urlopen(request, timeout=90) as response:
        pip.write_bytes(response.read())
    command = [str(python), '-B', str(pip), 'install', '--index-url', 'https://pypi.org/simple',
               '--no-cache-dir', '--no-compile', '--disable-pip-version-check', '--progress-bar', 'off',
               '--target', str(deps), '-r', str(root / 'requirements-portable.lock')]
    process = subprocess.run(command, stdout=sys.stderr, stderr=sys.stderr,
                             env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    if process.returncode:
        raise InstallError('DEPENDENCY_INSTALL_FAILED: 未修改助手配置，可重新运行安装入口。')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--clients', default='Generic', help=','.join(CLIENTS))
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--check', action='store_true', help='只检查现有连接，不下载或改助手配置')
    parser.add_argument('--login', action='store_true', help='仅当确认未登录时打开正常登录窗口')
    parser.add_argument('--remember-login', action='store_true', help='用户主动选择保留本机登录')
    parser.add_argument('--ref', default='main')
    parser.add_argument('--source', type=Path, help='已有完整源码目录；用于离线安装')
    options = parser.parse_args(argv)
    if options.remember_login and not options.login:
        parser.error('--remember-login requires --login')
    clients = [part.strip() for part in options.clients.split(',')]
    if not clients or len(set(clients)) != len(clients) or any(c not in CLIENTS for c in clients):
        parser.error('请选择受支持客户端；其它 stdio MCP 客户端使用 Generic')
    if not options.ref or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-' for c in options.ref):
        parser.error('ref must be a simple branch, tag or commit')
    home = Path.home()
    selected = existing_install(home)
    if selected is None and options.check:
        print(json.dumps({'state': 'NOT_INSTALLED', 'action': 'install_required'}))
        return 3
    if selected is None and not options.apply:
        print(json.dumps({'state': 'NOT_INSTALLED', 'action': 'run_with_apply', 'clients': clients}))
        return 3
    if selected:
        root, python = selected
    elif options.source:
        root, python = options.source.resolve(), Path(sys.executable).resolve()
        if not (root / 'run.py').is_file() or not (root / 'src/lingxi_connector/setup.py').is_file():
            raise InstallError('INVALID_SOURCE_DIRECTORY')
        # Explicit source mode expects dependencies already installed by its owner.
    else:
        root = Path(os.environ.get('LOCALAPPDATA', str(home / 'AppData/Local'))) / 'LingxiMemoryConnector/product'
        if root.exists():
            raise InstallError('UNREGISTERED_PRODUCT_EXISTS: 保留已有目录，请先核查，未覆盖。')
        root.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix='install-', dir=root.parent))
        commit = download_sources(staging, options.ref)
        (staging / 'runtime').mkdir(exist_ok=True)
        install_dependencies(staging, Path(sys.executable))
        (staging / 'runtime/source.json').write_text(json.dumps({'repository': REPOSITORY, 'commit': commit}), encoding='utf-8')
        staging.rename(root)
        python = Path(sys.executable).resolve()
    # Re-enter the registered runtime rather than accidentally changing interpreter ABI.
    if python.resolve() != Path(sys.executable).resolve():
        return subprocess.call([str(python), '-B', str(Path(__file__).resolve()), *(argv if argv is not None else sys.argv[1:])])
    runpy.run_path(str(root / 'run.py'), run_name='connector_agent_bootstrap')
    from lingxi_connector.broker_client import call
    from lingxi_connector.cloud import ConnectorError
    result = {'reused_install': selected is not None, 'root': str(root)}
    if not options.check:
        result['configuration'] = configure_clients(root, home, python, clients, apply=options.apply)
        if not options.apply:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
    try:
        result['connection'] = check_connection(root, call, options.login, options.remember_login)
    except ConnectorError as error:
        result['connection'] = {'action': 'check_error', 'error': error.code}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['connection']['action'] == 'ready' else 3


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    try:
        raise SystemExit(main())
    except (InstallError, OSError, ValueError, subprocess.SubprocessError) as error:
        message = str(error) if isinstance(error, InstallError) else type(error).__name__
        print(json.dumps({'error': message}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
