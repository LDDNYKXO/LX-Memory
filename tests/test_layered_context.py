"""Layering contract: audit growth cannot evict required constraints."""
import asyncio
import copy
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from lingxi_connector.context import CONTEXT_CACHE_MAX_ENTRIES, _empty_result, _fit_output, build_context
from lingxi_connector.broker import Broker
from lingxi_connector.context_audit import AUDIT_MAX_SNAPSHOTS, AuditSnapshots
from lingxi_connector.context_output import background
from lingxi_connector.cloud import ConnectorError
from test_context import make_fetch, page, search_hit


def fixture():
    r = _empty_result('layered-fixture')
    path = 'wiki/projects/example'
    r['project'].update(selected=path, title='Example', policy_evaluated_at='2026-10-17T00:00:00+00:00',
                        retrieval_decisions=[{'path': path, 'decision': 'selected'}])
    r.update(goal='减少重述', next_step='仅设计，不部署', constraints=['禁止保存。', '不得自动发布。'])
    r['progress'].update(current=['隔离实现待审阅'], pending_verification=['宿主未验证'])
    r['sources'] = [dict(path=p, source='lingxi_cloud', content_scope='full', content='原始正文',
                         updated=123, retrieved_at='2026-10-17T00:00:00+00:00')
                    for p in (path, 'core/核心记忆')]
    for key in ('goal', 'next_step', 'constraints', 'progress.current', 'progress.pending_verification'):
        r['field_sources'][key] = {'path': path, 'heading': key}
    return r


def wire_chars(data):
    # Independent final MCP content envelope, not the implementation's size helper.
    return len(json.dumps({'content': [{'type': 'text', 'text': json.dumps(data, ensure_ascii=False)}],
                           'isError': False, 'resultType': 'complete'}, ensure_ascii=False))


class BackgroundLayerTests(unittest.TestCase):
    def test_audit_growth_cannot_remove_constraints(self):
        r = fixture()
        r['project']['retrieval_decisions'] *= 2000
        _fit_output(r)
        self.assertEqual(r['constraints'], ['禁止保存。', '不得自动发布。'])
        self.assertEqual(r['state'], 'complete')
        self.assertNotIn('retrieval_decisions', r['project'])
        self.assertNotIn('content', r['sources'][0])
        self.assertNotIn('content_scope', r['sources'][0])
        self.assertEqual(r['essential']['constraints_status'], 'complete')
        self.assertLessEqual(wire_chars(r), 8000)
        self.assertEqual(r['budget']['output_chars'], wire_chars(r))

    def test_required_overflow_is_atomic_and_honest(self):
        for field in ('constraints', 'unknowns', 'conflicts'):
            with self.subTest(field=field):
                r = fixture(); r[field] = ['不可遗漏的条件。' * 3000]
                _fit_output(r)
                self.assertEqual(r['state'], 'partial')
                self.assertEqual(r['essential']['status'], 'budget_exceeded')
                self.assertEqual(r['essential']['constraints_status'], 'budget_exceeded')
                self.assertEqual(r['goal'], '')
                self.assertIn(field, r['essential']['missing_fields'])
                self.assertTrue(any('不得依赖缺失' in x for x in r['unknowns']))
                self.assertLessEqual(wire_chars(r), 8000)

    def test_escaping_and_repeated_fitting_are_counted(self):
        r = fixture(); r['constraints'] = [('条件\n"引号"\\路径😀' * 140)]
        _fit_output(r); before = copy.deepcopy(r); _fit_output(r)
        self.assertEqual(r, before)
        self.assertEqual(r['budget']['output_chars'], wire_chars(r))
        self.assertLessEqual(wire_chars(r), 8000)

    def test_history_only_progress_is_never_promoted_to_current(self):
        r = fixture(); r['progress']['current'] = []; r['progress']['executed'] = ['仅完成隔离代码']
        r['field_sources']['progress.executed'] = {'path': 'wiki/projects/example', 'heading': '已完成'}
        _fit_output(r)
        self.assertEqual(r['progress']['executed'], [])
        self.assertEqual(r['progress']['current'], [])
        self.assertEqual(r['state'], 'partial')
        self.assertIn('progress.current', r['essential']['missing_fields'])

    def test_failure_states_are_not_promoted_and_conflicts_stay_inline(self):
        for state in ('partial', 'conflict', 'project_ambiguous', 'service_unavailable'):
            r = fixture(); r['state'] = state; r['conflicts'] = ['结论有冲突']
            _fit_output(r)
            self.assertEqual(r['state'], state)
            self.assertEqual(r['conflicts'], ['结论有冲突'])

    def test_budget_edge_depends_on_final_envelope_not_inner_json(self):
        exact = None
        for n in range(5700, 7600):
            r = fixture(); r['constraints'] = ['不发布。' + 'x' * n]
            _fit_output(r)
            if r['state'] == 'complete' and wire_chars(r) == 8000:
                exact = r
                break
        self.assertIsNotNone(exact, 'fixture must hit the exact boundary')
        r = fixture(); r['constraints'] = [exact['constraints'][0] + 'x']
        _fit_output(r)
        self.assertEqual(r['essential']['status'], 'budget_exceeded')

    def test_audit_data_unavailable_does_not_make_background_incomplete(self):
        r = fixture(); _fit_output(r)
        self.assertEqual(r['audit']['status'], 'unavailable')
        self.assertEqual(r['essential']['status'], 'complete')


class ConstraintCoverageTests(unittest.IsolatedAsyncioTestCase):
    def fetch(self, boundary=True):
        content = '### 目标\n减少重述\n### 当前状态\n隔离完成\n### 下一步\n等待审阅\n'
        if boundary:
            content += '### 边界\n不发布。'
        return make_fetch(hits=[search_hit('wiki/projects/example', title='Example')], pages={
            'wiki/projects/example': page('wiki/projects/example', content=content),
            'core/核心记忆': page('core/核心记忆', content='### 规则\n不自动保存。')})

    async def test_both_constraint_groups_have_origins(self):
        r = await build_context(self.fetch(), project='Example')
        self.assertEqual(r['state'], 'complete')
        self.assertEqual(r['constraints'], ['不发布。', '不自动保存。'])
        origins = r['field_sources']['constraints']['origins']
        self.assertEqual({x['path'] for x in origins}, {'wiki/projects/example', 'core/核心记忆'})

    async def test_missing_project_boundary_never_means_no_constraints(self):
        r = await build_context(self.fetch(False), project='Example')
        self.assertEqual(r['state'], 'partial')
        self.assertEqual(r['essential']['constraints_status'], 'unknown')
        self.assertIn('constraints', r['essential']['missing_fields'])

    async def test_explicit_empty_constraint_declaration_is_known(self):
        fetch = make_fetch(hits=[search_hit('wiki/projects/example', title='Example')], pages={
            'wiki/projects/example': page('wiki/projects/example', content='### 当前状态\nP', goal='G', next_step='N', constraints=[]),
            'core/核心记忆': page('core/核心记忆', constraints=[])})
        r = await build_context(fetch, project='Example')
        self.assertEqual(r['state'], 'complete')
        self.assertEqual(r['constraints'], [])
        self.assertEqual(r['essential']['constraints_status'], 'complete')

    async def test_malformed_constraint_declarations_never_mean_no_constraints(self):
        for value in (None, {}, [None], [42], '', '  ', False, ['valid', None]):
            with self.subTest(value=value):
                fetch = make_fetch(hits=[search_hit('wiki/projects/example', title='Example')], pages={
                    'wiki/projects/example': page('wiki/projects/example', content='### 当前状态\nP',
                        goal='G', next_step='N', constraints=value),
                    'core/核心记忆': page('core/核心记忆', constraints=[])})
                r = await build_context(fetch, project='Example')
                self.assertEqual(r['state'], 'partial')
                self.assertEqual(r['essential']['constraints_status'], 'unknown')

    async def test_partial_scope_on_required_page_downgrades_constraints(self):
        path = 'wiki/projects/example'
        core = 'core/核心记忆'
        project = page(path, content='### 目标\nG\n### 当前状态\nP\n### 下一步\nN\n### 边界\n不发布。')
        core_page = page(core, content='### 规则\n不自动保存。', constraints=['不自动保存。'])
        core_page['content_scope'] = 'partial'  # incomplete scope without truncated=true
        fetch = make_fetch(hits=[search_hit(path, title='Example')],
                           pages={path: project, core: core_page})
        r = await build_context(fetch, project='Example')
        self.assertEqual(r['state'], 'partial')
        self.assertEqual(r['essential']['constraints_status'], 'unknown')

    async def test_partial_scope_on_candidate_page_downgrades_constraints(self):
        # The selected page is read in the candidate loop, which already downgrades a partial
        # scope; the final-plan check is a defensive duplicate for that path.
        path = 'wiki/projects/example'
        core = 'core/核心记忆'
        project = page(path, content='### 目标\nG\n### 当前状态\nP\n### 下一步\nN\n### 边界\n不发布。')
        project['content_scope'] = 'partial'
        core_page = page(core, content='### 规则\n不自动保存。', constraints=['不自动保存。'])
        fetch = make_fetch(hits=[search_hit(path, title='Example')],
                           pages={path: project, core: core_page})
        r = await build_context(fetch, project='Example')
        self.assertEqual(r['state'], 'partial')
        self.assertEqual(r['essential']['constraints_status'], 'unknown')

    async def test_non_cloud_required_page_downgrades_constraints(self):
        path = 'wiki/projects/example'
        core = 'core/核心记忆'
        project = page(path, content='### 目标\nG\n### 当前状态\nP\n### 下一步\nN\n### 边界\n不发布。')
        core_page = page(core, content='### 规则\n不自动保存。', constraints=['不自动保存。'])
        core_page['source'] = 'filesystem'
        fetch = make_fetch(hits=[search_hit(path, title='Example')],
                           pages={path: project, core: core_page})
        r = await build_context(fetch, project='Example')
        self.assertEqual(r['state'], 'partial')
        self.assertEqual(r['essential']['constraints_status'], 'unknown')

    async def test_missing_content_scope_on_required_page_is_conservative(self):
        path = 'wiki/projects/example'
        core = 'core/核心记忆'
        project = page(path, content='### 目标\nG\n### 当前状态\nP\n### 下一步\nN\n### 边界\n不发布。')
        core_page = page(core, content='### 规则\n不自动保存。', constraints=['不自动保存。'])
        core_page.pop('content_scope')
        fetch = make_fetch(hits=[search_hit(path, title='Example')],
                           pages={path: project, core: core_page})
        r = await build_context(fetch, project='Example')
        self.assertEqual(r['state'], 'partial')
        self.assertEqual(r['essential']['constraints_status'], 'unknown')


class AuditContractTests(unittest.IsolatedAsyncioTestCase):
    def broker(self):
        broker = Broker.__new__(Broker)
        broker.context_cache = {}
        broker.session = SimpleNamespace(epoch=0, state='CONNECTED')
        return broker

    async def test_same_snapshot_paging_has_no_new_retrieval_and_no_mutation(self):
        broker = self.broker(); raw = fixture()
        raw['sources'][0]['content'] = '正文\n"\\' * 6000
        with patch('lingxi_connector.broker.build_context', return_value=raw) as build:
            first = await broker.context({'request': '继续', 'project': 'Example', 'request_id': 'r1'})
            snap = first['audit']['snapshot_id']
            raw['sources'][0]['content'] = 'changed after snapshot'
            rows = []; cursor = ''
            for _ in range(100):
                result = await broker.context({'view': 'audit', 'snapshot_id': snap, 'cursor': cursor})
                self.assertEqual(result['snapshot_id'], snap)
                self.assertLessEqual(wire_chars(result), 8000)
                rows.extend(result['sections'])
                if not result['has_more']:
                    break
                cursor = result['next_cursor']
            else:
                self.fail('audit pagination did not finish')
        self.assertEqual(build.await_count, 1)
        source_parts = [x for x in rows if x['name'] == 'sources']
        restored = json.loads(''.join(x['content'] for x in source_parts))
        self.assertEqual(restored[0]['content'], '正文\n"\\' * 6000)

    async def test_account_session_change_rejects_snapshot(self):
        broker = self.broker()
        with patch('lingxi_connector.broker.build_context', return_value=fixture()):
            first = await broker.context({'request': '继续'})
        broker.session.epoch += 1
        with self.assertRaises(ConnectorError) as caught:
            await broker.context({'view': 'audit', 'snapshot_id': first['audit']['snapshot_id']})
        self.assertEqual(caught.exception.code, 'AUDIT_ACCESS_DENIED')

    async def test_expired_or_restarted_snapshot_never_refetches(self):
        broker = self.broker()
        with patch('lingxi_connector.broker.build_context', return_value=fixture()) as build:
            with patch('lingxi_connector.context_audit.time', SimpleNamespace(monotonic=lambda:100, time=lambda:100)):
                first = await broker.context({'request': '继续'})
            with patch('lingxi_connector.context_audit.time', SimpleNamespace(monotonic=lambda:1001, time=lambda:1001)):
                with self.assertRaises(ConnectorError) as caught:
                    await broker.context({'view': 'audit', 'snapshot_id': first['audit']['snapshot_id']})
        self.assertEqual(caught.exception.code, 'AUDIT_SNAPSHOT_EXPIRED')
        self.assertEqual(build.await_count, 1)

    async def test_bad_cursor_and_unknown_view_rejected(self):
        broker = self.broker()
        with patch('lingxi_connector.broker.build_context', return_value=fixture()):
            first = await broker.context({'request': '继续'})
        for args in ({'view': 'other'}, {'view': 'audit'},
                     {'view': 'audit', 'snapshot_id': first['audit']['snapshot_id'], 'cursor': 'forged'}):
            with self.subTest(args=args), self.assertRaises(ConnectorError):
                await broker.context(args)

    async def test_login_change_during_fetch_cannot_return_old_background(self):
        broker = self.broker()
        async def changing(*args, **kwargs):
            broker.session.epoch += 1
            return fixture()
        with patch('lingxi_connector.broker.build_context', side_effect=changing):
            with self.assertRaises(ConnectorError) as caught:
                await broker.context({'request': '继续'})
        self.assertEqual(caught.exception.code, 'ACCOUNT_CHANGED')

    async def test_another_snapshot_cursor_cannot_be_reused(self):
        broker = self.broker()
        with patch('lingxi_connector.broker.build_context', return_value=fixture()):
            a = await broker.context({'request': 'A'})
            b = await broker.context({'request': 'B'})
        first = await broker.context({'view': 'audit', 'snapshot_id': a['audit']['snapshot_id']})
        with self.assertRaises(ConnectorError) as caught:
            await broker.context({'view': 'audit', 'snapshot_id': b['audit']['snapshot_id'],
                                  'cursor': first['next_cursor']})
        self.assertEqual(caught.exception.code, 'INVALID_AUDIT_CURSOR')

    async def test_lru_eviction_never_refetches(self):
        broker = self.broker()
        with patch('lingxi_connector.broker.build_context', return_value=fixture()) as build:
            first = await broker.context({'request': '0'})
            for i in range(1, 9):
                await broker.context({'request': str(i)})
            with self.assertRaises(ConnectorError) as caught:
                await broker.context({'view': 'audit', 'snapshot_id': first['audit']['snapshot_id']})
        self.assertEqual(caught.exception.code, 'AUDIT_SNAPSHOT_EXPIRED')
        self.assertEqual(build.await_count, 9)

    async def test_cached_background_rebuilds_evicted_reference_without_retrieval(self):
        broker = self.broker()
        args = {'request':'0', 'request_id':'round-0'}
        with patch('lingxi_connector.broker.build_context', return_value=fixture()) as build:
            first = await broker.context(args)
            for i in range(1, 9):
                await broker.context({'request':str(i)})
            cached = await broker.context(args)
            self.assertNotEqual(first['audit']['snapshot_id'], cached['audit']['snapshot_id'])
            audit = await broker.context({'view':'audit', 'snapshot_id':cached['audit']['snapshot_id']})
            snapshot = broker.audit_snapshots.entries[cached['audit']['snapshot_id']]
            names = [name for name, _ in snapshot['sections']]
            self.assertNotIn('cached', names)
        self.assertTrue(cached['cached'])
        self.assertEqual(audit['snapshot_id'], cached['audit']['snapshot_id'])
        self.assertEqual(build.await_count, 9)


class McpEnvelopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_sdk_result_matches_measured_envelope_and_legacy_arguments(self):
        from pathlib import Path
        from lingxi_connector.mcp_server import create_server
        r = fixture(); _fit_output(r)
        with patch('lingxi_connector.mcp_server.call', return_value=r) as called:
            result = await create_server(Path('.')).call_tool('lingxi_memory_context', {'request': '继续'})
        envelope = result.model_dump(by_alias=True, exclude_none=True)
        self.assertEqual(r['budget']['output_chars'], len(json.dumps(envelope, ensure_ascii=False)))
        self.assertLessEqual(r['budget']['output_chars'], 8000)
        self.assertEqual(called.call_args.args[2], {'request':'继续','project':'','cwd':'','remote':'','request_id':''})

    async def test_audit_arguments_are_forwarded_without_calling_another_tool(self):
        from pathlib import Path
        from lingxi_connector.mcp_server import create_server
        with patch('lingxi_connector.mcp_server.call', return_value={'sections': []}) as called:
            await create_server(Path('.')).call_tool('lingxi_memory_context',
                {'request':'审计', 'view':'audit', 'snapshot_id':'snapshot', 'cursor':'cursor'})
        self.assertEqual(called.call_count, 1)
        self.assertEqual(called.call_args.args[1], 'context')
        self.assertEqual(called.call_args.args[2]['snapshot_id'], 'snapshot')

    async def test_audit_error_payload_is_textual_and_not_fatal(self):
        from pathlib import Path
        from lingxi_connector.mcp_server import create_server
        for code in ('AUDIT_SNAPSHOT_EXPIRED', 'AUDIT_ACCESS_DENIED'):
            with self.subTest(code=code):
                with patch('lingxi_connector.mcp_server.call',
                           side_effect=ConnectorError(code, 'denied')) as called:
                    result = await create_server(Path('.')).call_tool(
                        'lingxi_memory_context', {'request': '审计', 'view': 'audit', 'snapshot_id': 'x'})
                payload = result.model_dump(by_alias=True, exclude_none=True)
                self.assertFalse(payload['isError'])
                data = json.loads(payload['content'][0]['text'])
                self.assertEqual(data['error'], {'code': code, 'message': 'denied'})
                self.assertEqual(called.call_count, 1)


class CacheBoundTests(unittest.IsolatedAsyncioTestCase):
    def broker(self):
        broker = Broker.__new__(Broker)
        broker.context_cache = {}
        broker.session = SimpleNamespace(epoch=0, state='CONNECTED')
        return broker

    async def test_successful_cache_entries_are_bounded(self):
        broker = self.broker()
        with patch('lingxi_connector.broker.build_context', return_value=fixture()) as build:
            for i in range(50):
                await broker.context({'request': '继续', 'request_id': f'r{i}'})
        data_entries = [v for v in broker.context_cache.values() if 'data' in v]
        self.assertLessEqual(len(data_entries), CONTEXT_CACHE_MAX_ENTRIES)
        self.assertEqual(build.await_count, 50)

    def test_prune_never_drops_an_inflight_task(self):
        broker = self.broker()
        now = time.monotonic()
        for i in range(17):
            broker.context_cache[f'd{i}'] = {'data': {}, 'cached_at': now - 100 + i}
        broker.context_cache['inflight'] = {'task': object()}
        broker._prune_context_cache()
        self.assertIn('inflight', broker.context_cache)
        data_entries = [k for k, v in broker.context_cache.items() if 'data' in v]
        self.assertLessEqual(len(data_entries), CONTEXT_CACHE_MAX_ENTRIES)
        # `keep` must survive even when it is the oldest entry: without keep handling the
        # eviction would remove it first and this assertion would fail.
        broker.context_cache['keep-me'] = {'data': {}, 'cached_at': now - 200}
        broker._prune_context_cache(keep='keep-me')
        self.assertIn('keep-me', broker.context_cache)
        data_entries = [k for k, v in broker.context_cache.items() if 'data' in v]
        self.assertLessEqual(len(data_entries), CONTEXT_CACHE_MAX_ENTRIES)


class FailureEnvelopeShapeTests(unittest.TestCase):
    def test_failure_and_success_share_budget_and_consumption_keys(self):
        ok = background(fixture())
        raw = fixture()
        raw['constraints'] = ['不可遗漏的条件。' * 3000]
        failed = background(raw)
        self.assertEqual(failed['essential']['status'], 'budget_exceeded')
        self.assertEqual(set(failed['budget']), set(ok['budget']))
        self.assertEqual(set(failed['consumption']), set(ok['consumption']))
        self.assertIsNone(failed['consumption']['background_sources'])
        self.assertEqual(failed['history_delivery'], 'audit_only')

    def test_ambiguous_failure_keeps_actionable_hint(self):
        raw = fixture(); raw['state'] = 'project_ambiguous'
        raw['constraints'] = ['不可遗漏的条件。' * 3000]
        failed = background(raw)
        self.assertEqual(failed['state'], 'project_ambiguous')
        self.assertIn('歧义候选未随失败信封返回', failed['unknowns'][0])

    def test_background_does_not_mutate_raw(self):
        raw = fixture()
        before = copy.deepcopy(raw)
        background(raw)
        self.assertEqual(raw, before)

    def test_conflict_state_does_not_rewrite_constraints_status(self):
        unknown = fixture(); unknown['state'] = 'conflict'; unknown['_constraints_status'] = 'unknown'
        self.assertEqual(background(unknown)['essential']['constraints_status'], 'unknown')
        complete = fixture(); complete['state'] = 'conflict'; complete['_constraints_status'] = 'complete'
        self.assertEqual(background(complete)['essential']['constraints_status'], 'complete')

    def test_sources_metadata_is_not_aliased_to_raw(self):
        raw = fixture()
        raw['sources'][0]['updated'] = {'nested': [1]}
        out = background(raw)
        out['sources'][0]['updated']['nested'].append(2)
        self.assertEqual(raw['sources'][0]['updated'], {'nested': [1]})

    def test_failure_path_does_not_mutate_raw(self):
        raw = fixture(); raw['constraints'] = ['不可遗漏的条件。' * 3000]
        before = copy.deepcopy(raw)
        background(raw)
        self.assertEqual(raw, before)


class AuditCursorLruTests(unittest.TestCase):
    def test_paging_a_snapshot_protects_it_from_insertion_order_eviction(self):
        snapshots = AuditSnapshots()
        scope = ('session', 0)
        first = snapshots.store({'x': 'a' * 100}, scope)['snapshot_id']
        second = snapshots.store({'x': 'b' * 100}, scope)['snapshot_id']
        snapshots.read(first, scope)
        for _ in range(AUDIT_MAX_SNAPSHOTS - 1):
            snapshots.store({'x': 'c' * 100}, scope)
        self.assertTrue(snapshots.available({'snapshot_id': first}, scope))
        self.assertFalse(snapshots.available({'snapshot_id': second}, scope))

    def test_failed_cursor_does_not_protect_snapshot(self):
        snapshots = AuditSnapshots()
        scope = ('session', 0)
        first = snapshots.store({'x': 'a' * 100}, scope)['snapshot_id']
        snapshots.store({'x': 'b' * 100}, scope)
        with self.assertRaises(ConnectorError):
            snapshots.read(first, scope, cursor='forged')
        for _ in range(AUDIT_MAX_SNAPSHOTS - 1):
            snapshots.store({'x': 'c' * 100}, scope)
        self.assertFalse(snapshots.available({'snapshot_id': first}, scope))

    def test_budget_too_small_does_not_protect_snapshot(self):
        snapshots = AuditSnapshots()
        scope = ('session', 0)
        first = snapshots.store({'x': 'a' * 100}, scope)['snapshot_id']
        snapshots.store({'x': 'b' * 100}, scope)
        with self.assertRaises(ConnectorError) as caught:
            snapshots.read(first, scope, limit=1)
        self.assertEqual(caught.exception.code, 'AUDIT_BUDGET_TOO_SMALL')
        for _ in range(AUDIT_MAX_SNAPSHOTS - 1):
            snapshots.store({'x': 'c' * 100}, scope)
        self.assertFalse(snapshots.available({'snapshot_id': first}, scope))
