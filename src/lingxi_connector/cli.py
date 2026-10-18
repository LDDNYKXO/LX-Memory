import argparse
import json
import sys
from .broker_client import call
from .cloud import ConnectorError

def main(root):
    parser = argparse.ArgumentParser(description='灵犀记忆：独立登录、检索及授权写回预览')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('hook', help='读取标准事件 JSON 并返回只读背景注入；不采集聊天')
    sub.add_parser('integration-status', help='只读核对能力、配置与实际限制；不启动云端')
    rules = sub.add_parser('configure-rules', help='预览或安装统一规则块（保留已有规则）')
    rules.add_argument('--clients', required=True)
    rules.add_argument('--apply', action='store_true')
    hooks = sub.add_parser('configure-lifecycle', help='预览或配置只读模型前接续入口')
    hooks.add_argument('--clients', required=True)
    hooks.add_argument('--apply', action='store_true')
    rollback = sub.add_parser('rollback-rules', help='按修改前原文恢复本产品规则；遇到后续改动停止')
    rollback.add_argument('journal')
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
    context = sub.add_parser('context', help='读取统一背景（只读）；项目名、目录与仓库标识只作线索，不扫描')
    context.add_argument('request')
    context.add_argument('--project', default='')
    context.add_argument('--cwd', default='')
    context.add_argument('--remote', default='')
    context.add_argument('--request-id', default='')
    args = parser.parse_args()
    if args.command == 'hook':
        from .lifecycle import handle_event
        getattr(sys,'_lx_release_startup',lambda:None)()
        try:
            event = json.loads(sys.stdin.read(16385))
            if not isinstance(event, dict):
                raise ValueError()
            result = handle_event(root, event)
        except (ValueError, TypeError):
            result = {'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit',
                      'additionalContext': '灵犀背景 service_unavailable：事件格式不支持；不能宣称接续通过。'}}
        print(json.dumps(result, ensure_ascii=False))
        return
    if args.command in ('integration-status', 'configure-rules', 'configure-lifecycle', 'rollback-rules'):
        from pathlib import Path
        from .integration import capability_matrix, configure_rules, configure_lifecycle, rollback_rules
        from .setup import InstallerError
        getattr(sys,'_lx_release_startup',lambda:None)()
        try:
            if args.command == 'integration-status':
                result = capability_matrix(Path.home(), root)
            elif args.command == 'configure-rules':
                result = configure_rules(root, Path.home(), args.clients.split(','), apply=args.apply)
            elif args.command == 'configure-lifecycle':
                result = configure_lifecycle(root, Path.home(), sys.executable, args.clients.split(','), apply=args.apply)
            else:
                result = rollback_rules(root, Path.home(), args.journal)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except (InstallerError, ValueError, OSError) as error:
            print(json.dumps({'error': 'INTEGRATION_FAILED', 'detail': str(error)}, ensure_ascii=False))
            sys.exit(2)
        return
    if args.command == 'serve':
        from .broker import serve
        serve(root)
        return
    if args.command == 'mcp':
        from .mcp_server import create_server
        server=create_server(root)
        getattr(sys,'_lx_release_startup',lambda:None)()
        server.run(transport='stdio')
        return
    if args.command == 'config':
        print(json.dumps({'mcpServers': {'lingxi-memory': {'command': sys.executable, 'args': ['-B', str(root / 'run.py'), 'mcp'], 'env': {'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONIOENCODING': 'utf-8'}}}}, ensure_ascii=False, indent=2))
        return
    getattr(sys,'_lx_release_startup',lambda:None)()
    kwargs = {}
    if args.command == 'login':
        kwargs = {'remember': args.remember}
    elif args.command == 'search':
        kwargs = {'query': args.query, 'limit': args.limit}
    elif args.command == 'read':
        kwargs = {'path': args.path, 'offset': args.offset, 'max_chars': args.max_chars}
    elif args.command == 'context':
        kwargs = {'request': args.request, 'project': args.project, 'cwd': args.cwd,
                  'remote': args.remote, 'request_id': args.request_id}
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
