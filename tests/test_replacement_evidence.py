"""Bounded explicit-link evidence, entirely synthetic and read-only."""
import asyncio
import json
import unittest
from unittest.mock import patch

from lingxi_connector.context import build_context
from test_context import make_fetch, page, search_hit
from lingxi_connector.context import _build_context as build_context  # Raw retrieval; layering tested separately.



class ReplacementEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, links, returned, overrides=None, fail_reads=()):
        paths = {name: 'wiki/projects/example-' + name for name in links}
        pages = {'core/核心记忆': page('core/核心记忆', content='### 边界\n不保存或发布。')}
        for name, targets in links.items():
            fields = {'schema': 1, 'project': 'Example', 'supersedes': [paths.get(x, x) for x in targets],
                      'as_of': '2026-10-16T08:00:00+08:00', 'record_kind': 'handoff'}
            fields.update((overrides or {}).get(name, {}))
            body = '<!-- memory-project-record ' + json.dumps(fields, ensure_ascii=False) + ' -->\n'
            body += '### 目标\nGoal ' + name + '\n### 当前状态\nState ' + name + '\n### 下一步\nNext ' + name
            pages[paths[name]] = page(paths[name], content=body)
        fetch = make_fetch(hits=[search_hit(paths[n], title='Example ' + n) for n in returned],
                           pages=pages, fail_reads=[paths.get(n, n) for n in fail_reads])
        return fetch, paths, pages

    async def run_graph(self, links, returned, **kwargs):
        fetch, paths, _ = self.fixture(links, returned, **kwargs)
        result = await build_context(fetch, project='Example', request='继续这个项目')
        self.assert_budget(result)
        self.assertTrue(all(op in ('status', 'search', 'read') for op, _ in fetch.operations))
        self.assertEqual(result['budget']['search_attempts'], 1)
        initial = {paths[n] for n in returned} | {'core/核心记忆'}
        supplemental = [a['path'] for op, a in fetch.operations if op == 'read' and a['path'] not in initial]
        self.assertLessEqual(len(supplemental), 2)
        self.assertEqual(len(supplemental), len(set(supplemental)))
        return result, fetch, paths, supplemental

    def assert_budget(self, result):
        self.assertLessEqual(result['budget']['chars'], 40000)

    async def test_one_missing_bridge_resolves_without_an_extra_search(self):
        r, f, p, extra = await self.run_graph({'old': [], 'mid': ['old'], 'new': ['mid']}, ['old', 'new'])
        self.assertEqual(r['state'], 'complete')
        self.assertEqual(r['project']['selected'], p['new'])
        self.assertEqual(extra, [p['mid']])
        self.assertIn(p['mid'], r['project']['version_resolution']['considered'])
        self.assertEqual(r['progress']['current'], ['State new'])
        self.assertEqual(r['next_step'], 'Next new')

    async def test_two_missing_bridges_resolve_and_do_not_supply_current_fields(self):
        r, _, p, extra = await self.run_graph(
            {'old': [], 'm1': ['old'], 'm2': ['m1'], 'new': ['m2']}, ['old', 'new'])
        self.assertEqual(r['state'], 'complete')
        self.assertEqual(extra, [p['m2'], p['m1']])
        self.assertEqual(r['goal'], 'Goal new')
        self.assertEqual(r['progress']['current'], ['State new'])
        self.assertTrue(all(v['path'] == p['new'] for k, v in r['field_sources'].items() if k != 'constraints'))

    async def test_three_missing_bridges_stop_at_two_and_report_version_evidence(self):
        r, _, p, extra = await self.run_graph(
            {'old': [], 'm1': ['old'], 'm2': ['m1'], 'm3': ['m2'], 'new': ['m3']}, ['old', 'new'])
        self.assertEqual(extra, [p['m3'], p['m2']])
        self.assertEqual(r['state'], 'partial')
        self.assertIsNone(r['project']['selected'])
        self.assertEqual(r['project']['evidence_gap']['kind'], 'replacement_chain')
        self.assertNotIn('哪一个项目', r['next_step'])

    async def test_an_already_complete_chain_does_not_read_historical_tails(self):
        r, _, p, extra = await self.run_graph(
            {'tail': [], 'old': ['tail'], 'new': ['old']}, ['old', 'new'])
        self.assertEqual(r['state'], 'complete')
        self.assertEqual(extra, [])
        self.assertNotIn('replacement_evidence', r['project'])

    async def test_frontier_prioritizes_evidence_connecting_existing_candidate_components(self):
        r, _, p, extra = await self.run_graph(
            {'tail': [], 'old': ['tail'], 'bridge': ['old'], 'mid': ['bridge'], 'new': ['mid']},
            ['old', 'mid', 'new'])
        self.assertEqual(r['state'], 'complete')
        self.assertEqual(extra, [p['bridge']])

    async def test_shared_reference_is_read_once(self):
        r, _, p, extra = await self.run_graph(
            {'old': [], 'bridge': ['old'], 'mid': ['bridge'], 'new': ['mid', 'bridge']}, ['old', 'mid', 'new'])
        self.assertEqual(r['state'], 'complete')
        self.assertEqual(extra, [p['bridge']])

    async def test_failed_supplement_is_not_retried(self):
        r, _, p, extra = await self.run_graph({'old': [], 'mid': ['old'], 'new': ['mid']},
                                              ['old', 'new'], fail_reads=('mid',))
        self.assertEqual(extra, [p['mid']])
        self.assertEqual(r['state'], 'partial')
        self.assertIsNone(r['project']['selected'])

    async def test_foreign_reference_is_rejected_without_exposing_its_body(self):
        r, _, p, extra = await self.run_graph({'old': [], 'mid': ['old'], 'new': ['mid']},
            ['old', 'new'], overrides={'mid': {'project': 'Other'}})
        self.assertEqual(extra, [p['mid']])
        self.assertNotEqual(r['state'], 'complete')
        self.assertIsNone(r['project']['selected'])
        self.assertFalse(any(s.get('content') for s in r['sources'] if s['path'] == p['mid']))

    async def test_history_test_and_inapplicable_bridges_are_not_promoted(self):
        for meta in ({'record_kind': 'test'}, {'record_kind': 'history'},
                     {'valid_from': '2099-01-01T00:00:00Z'}, {'valid_until': '2026-10-16T09:00:00Z'}):
            with self.subTest(meta=meta):
                r, _, p, extra = await self.run_graph({'old': [], 'mid': ['old'], 'new': ['mid']},
                    ['old', 'new'], overrides={'mid': meta})
                self.assertEqual(r['state'], 'partial')
                self.assertIsNone(r['project']['selected'])
                self.assertEqual(extra, [p['mid']])

    async def test_supplement_reveals_a_cycle(self):
        r, _, p, extra = await self.run_graph({'old': [], 'mid': ['new'], 'new': ['mid']}, ['old', 'new'])
        self.assertEqual(r['state'], 'conflict')
        self.assertIsNone(r['project']['selected'])
        self.assertEqual(extra, [p['mid']])

    async def test_supplement_reveals_competing_replacements(self):
        r, _, p, extra = await self.run_graph(
            {'old': [], 'mid': ['old'], 'a': ['mid'], 'b': ['mid']}, ['old', 'a', 'b'])
        self.assertEqual(r['state'], 'conflict')
        self.assertIsNone(r['project']['selected'])
        self.assertEqual(extra, [p['mid']])

    async def test_same_project_without_replacement_proof_does_not_ask_for_project_name(self):
        r, _, _, extra = await self.run_graph({'a': [], 'b': []}, ['a', 'b'])
        self.assertEqual(r['state'], 'partial')
        self.assertEqual(extra, [])
        self.assertEqual(r['project']['evidence_gap']['kind'], 'replacement_chain')
        self.assertNotIn('哪一个项目', r['next_step'])

    async def test_real_identity_ambiguity_remains_project_ambiguous(self):
        f = make_fetch(hits=[search_hit('wiki/projects/a', title='Example'),
                            search_hit('wiki/projects/b', title='Example')],
                       pages={p: page(p, content='### 目标\nG') for p in ('wiki/projects/a', 'wiki/projects/b')})
        r = await build_context(f, project='Example')
        self.assertEqual(r['state'], 'project_ambiguous')

    async def test_character_budget_exhaustion_does_not_start_a_supplement(self):
        f, p, pages = self.fixture({'old': [], 'mid': ['old'], 'new': ['mid']}, ['old', 'new'])
        cap = sum(len(pages[p[n]]['content']) for n in ('old', 'new'))
        with patch('lingxi_connector.context.CONTEXT_TOTAL_CHARS', cap):
            r = await build_context(f, project='Example')
        self.assertEqual(r['state'], 'partial')
        self.assertEqual(r['budget']['chars'], cap)
        self.assertFalse(any(a.get('path') == p['mid'] for _, a in f.operations))

    async def test_truncated_supplement_cannot_resolve_the_chain(self):
        f, p, pages = self.fixture({'old': [], 'mid': ['old'], 'new': ['mid']}, ['old', 'new'])
        cap = sum(len(pages[p[n]]['content']) for n in ('old', 'new')) + 20
        with patch('lingxi_connector.context.CONTEXT_TOTAL_CHARS', cap):
            r = await build_context(f, project='Example')
        self.assertEqual(r['state'], 'partial')
        self.assertIsNone(r['project']['selected'])
        self.assertLessEqual(r['budget']['chars'], cap)

    async def test_slow_supplement_uses_remaining_shared_time_budget(self):
        f, p, _ = self.fixture({'old': [], 'mid': ['old'], 'new': ['mid']}, ['old', 'new'])
        async def fetch(op, args):
            if op == 'read' and args['path'] == p['mid']:
                await asyncio.sleep(.15)
            return await f(op, args)
        with patch('lingxi_connector.context.CONTEXT_TOTAL_TIMEOUT_S', .05):
            r = await asyncio.wait_for(build_context(fetch, project='Example'), timeout=.13)
        self.assertEqual(r['state'], 'partial')
        self.assertIsNone(r['project']['selected'])

    async def test_encoded_reference_is_rejected_before_reading(self):
        bad = 'wiki/projects/%2e%2e/core/private'
        r, f, _, extra = await self.run_graph({'old': [], 'new': [bad]}, ['old', 'new'])
        self.assertEqual(r['state'], 'partial')
        self.assertEqual(extra, [])
        self.assertFalse(any(a.get('path') == bad for _, a in f.operations))

    async def test_response_for_a_different_path_is_not_bridge_evidence(self):
        f, p, _ = self.fixture({'old': [], 'mid': ['old'], 'new': ['mid']}, ['old', 'new'])
        async def fetch(op, args):
            value = await f(op, args)
            return {**value, 'path': 'wiki/projects/something-else'} if op == 'read' and args['path'] == p['mid'] else value
        r = await build_context(fetch, project='Example')
        self.assertEqual(r['state'], 'partial')
        self.assertIsNone(r['project']['selected'])

    async def test_bridge_cannot_disagree_with_the_known_repository(self):
        f, p, _ = self.fixture({'old': [], 'mid': ['old'], 'new': ['mid']}, ['old', 'new'])
        async def fetch(op, args):
            value = await f(op, args)
            if op == 'read':
                remote = 'github.com/other/example' if args['path'] == p['mid'] else 'github.com/owner/example'
                return {**value, 'front_matter': {**value['front_matter'], 'remote': remote}}
            return value
        r = await build_context(fetch, project='Example')
        self.assertEqual(r['state'], 'partial')
        self.assertIsNone(r['project']['selected'])

    async def test_two_bridges_cannot_introduce_conflicting_repository_identities(self):
        f, p, _ = self.fixture({'old': [], 'm1': ['old'], 'm2': ['m1'], 'new': ['m2']}, ['old', 'new'])
        async def fetch(op, args):
            value = await f(op, args)
            remotes = {p['m1']: 'github.com/a/example', p['m2']: 'github.com/b/example'}
            if op == 'read' and args['path'] in remotes:
                value = {**value, 'front_matter': {**value['front_matter'], 'remote': remotes[args['path']]}}
            return value
        r = await build_context(fetch, project='Example')
        self.assertEqual(r['state'], 'partial')
        self.assertIsNone(r['project']['selected'])
        self.assertEqual(len(r['project']['replacement_evidence']['attempts']), 2)
        self.assertEqual(r['project']['replacement_evidence']['attempts'][-1]['state'], 'rejected')
