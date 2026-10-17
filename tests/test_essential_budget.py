"""Synthetic fixed-budget essentials, source integrity and honest overflow."""
import copy
import json
import unittest
from test_layered_context import wire_chars
from lingxi_connector.context import _fit_output


class EssentialIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_five_candidates_bridge_and_core_keep_boundaries(self):
        from test_context import make_fetch, page, search_hit
        from lingxi_connector.context import build_context
        paths = ['wiki/projects/example-' + str(i) + '-' + 'a' * 32 for i in range(6)]
        pages = {}
        boundary = '禁止部署或保存；只有明确授权才可执行。' * 20
        core_boundary = '不得自动提交；不能把旧记录当实时状态。' * 25
        for i, path in enumerate(paths):
            marker = dict(schema=1, project='Example', record_kind='handoff',
                as_of='2026-10-17T00:00:00+00:00',
                supersedes=[paths[i-1]] if i else [])
            body = '<!-- memory-project-record ' + json.dumps(marker) + ' -->\n'
            body += ('### 目标\n' + '减少重述背景。' * 20 + '\n### 当前状态\n' + '仅工作树完成，尚未部署。' * 20
                     + '\n### 待验证\n' + '恢复尚未验收。' * 20 + '\n### 已完成\n' + '历史记录。' * 200
                     + '\n### 下一步\n等待授权。\n### 边界\n' + boundary)
            pages[path] = page(path, content=body)
        pages['core/核心记忆'] = page('core/核心记忆', content='### 边界\n' + core_boundary)
        fetch = make_fetch(hits=[search_hit(paths[i], title='Example') for i in (0,2,3,4,5)], pages=pages)
        raw = await build_context(fetch, project='Example', request='继续这个项目', raw=True)
        r = _fit_output(copy.deepcopy(raw))
        self.assertEqual(r['state'], 'complete')
        self.assertEqual(r['project']['selected'], paths[5])
        self.assertEqual(raw['project']['replacement_evidence']['state'], 'resolved')
        self.assertEqual(r['budget']['replacement_reads'], 1)
        self.assertEqual(r['constraints'], [boundary, core_boundary])
        self.assertEqual(r['budget']['search_attempts'], 1)
        self.assertEqual(len([a for op,a in fetch.operations if op=='read']), 7)
        self.assertTrue(all(op in ('status','search','read') for op,_ in fetch.operations))
        self.assertTrue(all(r['field_sources'][k]['path']==paths[5] for k in
            ('goal','next_step','progress.current','progress.pending_verification')))
        self.assertEqual(r['budget']['output_chars'],wire_chars(r))
        self.assertLessEqual(r['budget']['output_chars'],8000)


if __name__ == '__main__':
    unittest.main()
