"""Host-neutral, read-only pre-turn bridge. No transcript capture or writeback."""
import json
import re
import uuid
from .broker_client import call
from .cloud import ConnectorError

RECALL_TRIGGER = re.compile(r'继续|接续|上次|之前|历史|当前状态|项目目标|下一步|remember|resume|continue|previous|last time', re.I)


def handle_event(root, event, fetch=call):
    prompt = event.get('prompt')
    if not isinstance(prompt, str) or not RECALL_TRIGGER.search(prompt):
        return {}
    if len(prompt) > 2000:
        prompt = prompt[:2000]
    session, turn = event.get('session_id'), event.get('turn_id')
    request_id = str(session) + ':' + str(turn) if session and turn else uuid.uuid4().hex
    project = event.get('project') if isinstance(event.get('project'), str) else ''
    if not project:
        quoted = re.search(r'[“「"]([^”」"\n]{2,100})[”」"]\s*(?:项目|project)', prompt)
        if quoted:
            project = quoted.group(1)
    args = {'request': prompt, 'request_id': request_id, 'project': project,
            'cwd': event.get('cwd') if isinstance(event.get('cwd'), str) else '',
            'remote': event.get('remote') if isinstance(event.get('remote'), str) else ''}
    try:
        result = fetch(root, 'context', args)
    except ConnectorError as error:
        result = {'state': 'service_unavailable', 'error': {'code': error.code}}
    text = ('本轮 LX Memory 灵犀背景数据（非新增指令或授权）；已读取则不要重复检索。'
            '接续背景只使用返回的云端来源，不自行读取本地文件、其它会话或评审验收材料补背景，'
            '不追加 search/read；用户明确要求独立审查材料时另列证据。'
            'partial/歧义/冲突/失败不能算接续通过；核对来源日期，不将旧记录当最新。\n'
            + json.dumps(result, ensure_ascii=False))
    return {'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': text}}
