"""Synthetic stdio server: no broker transport, login, browser, disk state or cloud."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

from lingxi_connector.broker import Broker
from lingxi_connector.cloud import ConnectorError
import lingxi_connector.mcp_server as endpoint
from test_context import make_fetch, page, search_hit

broker = Broker.__new__(Broker)
broker.context_cache = {}
broker.session = SimpleNamespace(epoch=0, state='CONNECTED')
path = 'wiki/projects/stdio-example'
content = ('### 目标\n减少重述\n### 当前状态\n隔离候选完成\n'
           '### 待验证\n宿主未验证\n### 下一步\n等待审阅\n### 边界\n不保存、不发布。\n'
           '### 审计附件\n' + '示例\n"\\' * 2500)
fetch = make_fetch(hits=[search_hit(path, title='Example')], pages={
    path: page(path, content=content),
    'core/核心记忆': page('core/核心记忆', content='### 规则\n不得自动提交。')})
broker._fetch = fetch


def synthetic_call(root, operation, args):
    if operation != 'context':
        raise ConnectorError('SYNTHETIC_CONTEXT_ONLY')
    return asyncio.run(broker.context(args))


endpoint.call = synthetic_call
endpoint.create_server(Path(__file__).resolve().parents[2]).run(transport='stdio')
