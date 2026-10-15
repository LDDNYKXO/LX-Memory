import argparse
import json
import sys
from .broker_client import call
from .cloud import ConnectorError

def main(root):
    parser = argparse.ArgumentParser(description='灵犀记忆：独立登录、检索及授权写回预览')
    sub = parser.add_subparsers(dest='command', required=True)
    login = sub.add_parser('login', help='打开本产品的独立登录窗口')
    login.add_argument('--remember', action='store_true', help='主动选择保存本机登录，重启后可继续使用')
    for name in ('status', 'logout', 'stop', 'serve', 'mcp', 'config', 'write-status','enable-saving'):
        sub.add_parser(name)
    prepare = sub.add_parser('prepare',help='从JSON文件生成完整预览；不写云端')
    prepare.add_argument('input_file')
    for name in ('commit','receipt'):
        action=sub.add_parser(name)
        action.add_argument('proposal_id')
    search = sub.add_parser('search')
    search.add_argument('query')
    search.add_argument('--limit', type=int, default=5)
    read = sub.add_parser('read')
    read.add_argument('path')
    read.add_argument('--offset', type=int, default=0)
    read.add_argument('--max-chars', type=int, default=8000)
    args = parser.parse_args()
    if args.command == 'serve':
        from .broker import serve
        serve(root)
        return
    if args.command == 'mcp':
        from .mcp_server import create_server
        create_server(root).run(transport='stdio')
        return
    if args.command == 'config':
        print(json.dumps({'mcpServers': {'lingxi-memory': {'command': sys.executable, 'args': ['-B', str(root / 'run.py'), 'mcp'], 'env': {'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONIOENCODING': 'utf-8'}}}}, ensure_ascii=False, indent=2))
        return
    kwargs = {}
    if args.command == 'login':
        kwargs = {'remember': args.remember}
    elif args.command == 'search':
        kwargs = {'query': args.query, 'limit': args.limit}
    elif args.command == 'read':
        kwargs = {'path': args.path, 'offset': args.offset, 'max_chars': args.max_chars}
    elif args.command == 'prepare':
        from pathlib import Path
        try:
            kwargs=json.loads(Path(args.input_file).read_text(encoding='utf-8-sig'))
            if not isinstance(kwargs,dict) or set(kwargs)!={'category','title','summary','content'}:
                raise ValueError()
        except (ValueError,OSError):
            print(json.dumps({'error':{'code':'INVALID_INPUT'}}))
            sys.exit(2)
    elif args.command in ('commit','receipt'):
        kwargs={'proposal_id':args.proposal_id}
    try:
        result = call(root, args.command, kwargs, autostart=args.command != 'stop')
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except ConnectorError as e:
        print(json.dumps({'error': {'code': e.code, 'message': str(e)}}, ensure_ascii=False))
        sys.exit(2)
