"""Regression tests for the product's user-visible continuity contract."""
import json
import unittest
import subprocess
import sys
from lingxi_connector.context import (OUTPUT_MAX_CHARS, _empty_result, _fit_output, build_context)
from test_layered_context import wire_chars
from lingxi_connector.project_identity import clues, search_evidence, select_project
from test_context import make_fetch, page


class ContextContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_candidate_audit_is_partial_without_corrupting_paths(self):
        path='wiki/projects/example'
        result=await build_context(make_fetch(hits=[{'path':path,'title':'Example '+'x'*20000}],
            pages={path:page(path,content='### 目标\nG\n### 当前状态\nS\n### 下一步\nN')}),project='Example')
        self.assertEqual(result['state'],'partial')
        self.assertLessEqual(len(json.dumps(result,ensure_ascii=False)),OUTPUT_MAX_CHARS)
        self.assertFalse(result['consumption']['latest_state_verified'])

    async def test_partial_candidate_does_not_expose_current_selection(self):
        path='wiki/projects/example'
        result=await build_context(make_fetch(hits=[{'path':path,'title':'Example'}],
            pages={path:page(path,content='### 目标\nG\n### 当前状态\nS\n### 下一步\nN',truncated=True)}),project='Example')
        self.assertEqual(result['state'],'partial')
        self.assertIsNone(result['project']['selected'])
        self.assertEqual(result['progress']['current'],[])

    async def test_personal_core_cannot_supply_missing_project_essentials(self):
        result = await build_context(make_fetch(hits=[{'path': 'wiki/projects/a', 'title': 'Example'}],
            pages={'wiki/projects/a': page('wiki/projects/a', content='Historical note'),
                   'core/核心记忆': page('core/核心记忆', content='### 目标\nPersonal goal\n### 当前状态\nPersonal state\n### 下一步\nPersonal next\n### 限制\nShared constraint')}), project='Example')
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['goal'], '')
        self.assertEqual(result['next_step'], '')
        self.assertIn('Shared constraint', result['constraints'])

    async def test_many_short_summaries_terminate_inside_output_budget(self):
        code = "from lingxi_connector.context import _fit_output,_empty_result; import json; r=_empty_result('test'); r['constraints']=[str(i)+'x'*129 for i in range(80)]; _fit_output(r); print(json.dumps(r,ensure_ascii=False))"
        completed = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True,
                                   text=True, encoding='utf-8', timeout=3,
                                   env={**__import__('os').environ, 'PYTHONIOENCODING': 'utf-8'})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), OUTPUT_MAX_CHARS)
        self.assertEqual(result['state'], 'partial')

    async def test_missing_project_never_claims_complete(self):
        result = await build_context(make_fetch(), request='继续这个项目')
        self.assertEqual(result['state'], 'partial')
        self.assertTrue(result['unknowns'])

    async def test_missing_goal_and_next_step_never_claims_complete(self):
        rows = [{'path': 'wiki/projects/a', 'title': 'Example', 'score': 99}]
        result = await build_context(make_fetch(hits=rows, pages={
            'wiki/projects/a': page('wiki/projects/a', content='Unstructured historical note'),
        }), request='Continue', project='Example')
        self.assertEqual(result['state'], 'partial')
        self.assertTrue(any('goal' in x for x in result['unknowns']))

    async def test_summary_itself_obeys_whole_output_budget(self):
        body = '### 目标\n' + '目' * 3500 + '\n### 当前状态\n' + '状' * 3500 + '\n### 下一步\n' + '步' * 3500
        result = await build_context(make_fetch(hits=[{'path': 'wiki/projects/a', 'title': 'Example'}],
            pages={'wiki/projects/a': page('wiki/projects/a', content=body)}), project='Example')
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), OUTPUT_MAX_CHARS)
        self.assertEqual(result['budget']['output_chars'], wire_chars(result))
        self.assertEqual(result['state'], 'partial')


class ProjectEvidenceContractTests(unittest.TestCase):
    def test_top_score_alone_is_not_project_identity(self):
        result = search_evidence(clues(project='Example'), 'Example', [
            {'path': 'wiki/projects/unrelated', 'title': 'Another product', 'score': 99}])
        self.assertEqual(result['state'], 'none')

    def test_two_matching_pages_require_clarification(self):
        result = search_evidence(clues(project='Example'), 'Example', [
            {'path': 'wiki/projects/a', 'title': 'Example current', 'score': 99},
            {'path': 'wiki/projects/b', 'title': 'Example historical', 'score': 98}])
        self.assertEqual(result['state'], 'project_ambiguous')
        self.assertEqual(len(result['candidates']), 2)

    def test_flat_namespace_does_not_merge_same_name_projects(self):
        result = select_project(clues(project='Example'), [
            {'path': 'wiki/projects/a', 'title': 'Example'},
            {'path': 'wiki/projects/b', 'title': 'Example'}])
        self.assertEqual(result['state'], 'project_ambiguous')
