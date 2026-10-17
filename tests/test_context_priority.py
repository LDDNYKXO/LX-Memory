"""Synthetic continuity priorities; no private pages, network, or installed hosts."""
import unittest

from lingxi_connector.context import _build_context as build_context
from test_context import make_fetch, page
from test_lx_memory import record


class PendingHeadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_headings_are_not_completed_progress(self):
        for heading in ('待验证', '待验证事项', '待验收', '待验证进展'):
            with self.subTest(heading=heading):
                path = 'wiki/projects/example'
                text = ('### 目标\n继续项目\n### 当前状态\n仅工作树完成\n'
                        '### %s\n未部署；尚未证明真实恢复。\n### 下一步\n等待确认\n' % heading)
                fetch = make_fetch(hits=[{'path': path, 'title': 'Example'}],
                                   pages={path: page(path, content=text)})
                result = await build_context(fetch, project='Example')
                self.assertEqual(result['state'], 'complete')
                self.assertEqual(result['progress']['pending_verification'], ['未部署；尚未证明真实恢复。'])
                self.assertEqual(result['progress']['executed'], [])
                self.assertEqual(result['field_sources']['progress.pending_verification'],
                                 {'path': path, 'heading': heading})
                self.assertEqual([op for op, _ in fetch.operations], ['status', 'search', 'read', 'read'])

    async def test_pending_heading_inside_fence_is_not_a_fact(self):
        path = 'wiki/projects/example'
        text = ('### 目标\n目标\n### 当前状态\n状态\n### 下一步\n下一步\n'
                '```markdown\n### 待验证\n示例不是事实\n```')
        result = await build_context(make_fetch(hits=[{'path': path, 'title': 'Example'}],
            pages={path: page(path, content=text)}), project='Example')
        self.assertEqual(result['progress']['pending_verification'], [])

    async def test_pending_evidence_alone_does_not_supply_current_progress(self):
        path = 'wiki/projects/example'
        result = await build_context(make_fetch(hits=[{'path': path, 'title': 'Example'}],
            pages={path: page(path, content='### 目标\n目标\n### 待验证\n当前状态未知\n### 下一步\n审阅')}),
            project='Example')
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['progress']['current'], [])
        self.assertTrue(result['progress']['pending_verification'])
        self.assertTrue(any('progress' in text for text in result['unknowns']))

    async def test_old_and_core_pending_fields_never_become_current_project_facts(self):
        old, new = 'wiki/projects/example-old', 'wiki/projects/example-new'
        pages = {
            old: page(old, content=record('Example', []) + '### 待验证\n历史任务未验收'),
            new: page(new, content=record('Example', [old]) +
                      '### 目标\n目标\n### 当前状态\n状态\n### 待验证\n本轮未部署\n### 下一步\n审阅'),
            'core/核心记忆': page('core/核心记忆', content='### 待验证\n其它项目的私人事项')}
        fetch = make_fetch(hits=[{'path': p, 'title': 'Example'} for p in (old, new)], pages=pages)
        result = await build_context(fetch, project='Example')
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(result['progress']['pending_verification'], ['本轮未部署'])
        self.assertEqual(result['field_sources']['progress.pending_verification']['path'], new)
        self.assertEqual([op for op, _ in fetch.operations], ['status', 'search', 'read', 'read', 'read'])
