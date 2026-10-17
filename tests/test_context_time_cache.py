"""Pure in-memory Broker cache checks, no network or session directory."""
import copy
import json
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from lingxi_connector.broker import Broker
from test_layered_context import wire_chars
from lingxi_connector.context import _empty_result


class TimeCacheTests(unittest.IsolatedAsyncioTestCase):
    async def check_boundary(self, boundary, state='complete', in_flight=False):
        broker=Broker.__new__(Broker)
        broker.context_cache={}
        broker.session=SimpleNamespace(epoch=0)
        start='2026-10-16T04:12:00+00:00'
        deadline='2026-10-16T04:12:01+00:00'
        value=_empty_result('r1')
        value.update(state=state,goal='G',next_step='N',constraints=['No deployment'])
        value['progress']['current']=['S']
        value['project'].update(selected='wiki/projects/a',policy_evaluated_at=start,
            retrieval_decisions=[{'path':'wiki/projects/a',boundary:deadline,'decision':'selected'}])
        stamp=datetime.fromisoformat(start).timestamp()
        now=[stamp]
        async def build(*args,**kwargs):
            if in_flight: now[0]+=2
            return copy.deepcopy(value)
        args={'request':'continue','project':'Example','request_id':'r1'}
        with patch('lingxi_connector.broker.build_context',side_effect=build) as call, \
             patch('lingxi_connector.broker.time.time',side_effect=lambda:now[0]):
            first=await broker.context(args)
            if not in_flight:
                self.assertEqual(first['state'],state)
                cached=await broker.context(args)
                self.assertEqual(cached['budget']['output_chars'],wire_chars(cached))
                now[0]+=2
            result=await broker.context(args)
        self.assertEqual(call.await_count,1,'same-turn expiry must not automatically repeat retrieval')
        self.assertEqual(result['state'],state if state in ('conflict','project_ambiguous') else 'partial')
        self.assertTrue(result['project'].get('policy_recheck_required'), 'time boundary was not rechecked')
        self.assertIsNone(result['project']['selected'])
        self.assertEqual(result['next_step'],'')
        self.assertEqual(result['progress']['current'],[])

    async def test_cached_complete_cannot_outlive_valid_until(self):
        await self.check_boundary('valid_until')

    async def test_future_partial_cache_cannot_hide_valid_from_boundary(self):
        await self.check_boundary('valid_from',state='partial')

    async def test_future_as_of_partial_cache_marks_recheck(self):
        await self.check_boundary('as_of',state='partial')

    async def test_in_flight_read_that_crossed_boundary_is_partial(self):
        await self.check_boundary('valid_until',in_flight=True)

    async def test_time_boundary_never_erases_existing_graph_conflict(self):
        await self.check_boundary('valid_until',state='conflict',in_flight=True)

    async def test_time_boundary_never_erases_project_ambiguity(self):
        await self.check_boundary('valid_until',state='project_ambiguous',in_flight=True)

    async def test_cached_answer_keeps_its_state_and_exact_output_count(self):
        broker=Broker.__new__(Broker)
        broker.context_cache={}
        broker.session=SimpleNamespace(epoch=0)
        value=_empty_result('r1')
        value.update(state='complete',goal='G'*60,next_step='N'*60,constraints=['No deployment'])
        value['progress']['current']=['S'*60]
        value['project'].update(selected='wiki/projects/a',policy_evaluated_at='2026-10-16T04:12:00+00:00')
        value['sources']=[{'path':'wiki/projects/a','chars':9000,'content':'C'*9000},
                          {'path':'core/核心记忆','chars':200,'content':'K'*200}]
        async def build(*args,**kwargs):
            return copy.deepcopy(value)
        args={'request':'continue','project':'Example','request_id':'r1'}
        with patch('lingxi_connector.broker.build_context',side_effect=build) as call:
            first=await broker.context(args)
            cached=await broker.context(args)
        self.assertEqual(call.await_count,1,'a same-round cache hit must not retrieve again')
        self.assertTrue(cached.get('cached'))
        for result in (first,cached):
            raw=json.dumps(result,ensure_ascii=False)
            self.assertLessEqual(len(raw),8000)
            self.assertEqual(result['budget']['output_chars'],wire_chars(result))
            self.assertEqual(result['state'],'complete')
        self.assertTrue(cached['goal'] and cached['next_step'])
        self.assertFalse(any('…' in item for item in cached['constraints']))
