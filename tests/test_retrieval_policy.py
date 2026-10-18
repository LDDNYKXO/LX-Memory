"""Offline policy scenarios; all pages and calls are synthetic."""
import itertools
import json
import unittest
from lingxi_connector.context import build_context, OUTPUT_MAX_CHARS
from test_context import make_fetch, page, search_hit
from test_record_policy import body
from lingxi_connector.context import _build_context as build_context  # Raw retrieval; layering tested separately.



def fixture(records, hit_metadata=None, page_metadata=None):
    pages={}; hits=[]
    for name, fields in records.items():
        path='wiki/projects/example-'+name
        fields=dict(fields)
        links=['wiki/projects/example-'+n for n in fields.pop('links',[])]
        title=fields.pop('title','Example '+name)
        content=body(supersedes=links,**fields)+'### 目标\nGoal '+name+'\n### 当前状态\nState '+name+'\n### 下一步\nNext '+name
        hits.append(search_hit(path,title=title,**(hit_metadata or {}).get(name,{})))
        pages[path]=page(path,content=content,**(page_metadata or {}).get(name,{}))
    return make_fetch(hits=hits,pages=pages),hits,pages


class RetrievalPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def run_records(self, records, **kwargs):
        f,_,_=fixture(records,**kwargs)
        r=await build_context(f,project='Example',remote='https://github.com/owner/example')
        self.assertTrue(all(op in ('status','search','read') for op,_ in f.operations))
        return r,f

    async def test_formal_and_test_choose_formal_with_reasons(self):
        r,_=await self.run_records({'formal':{'record_kind':'handoff'},'test':{'record_kind':'test'}})
        self.assertEqual(r['state'],'complete')
        self.assertEqual(r['progress']['current'],['State formal'])
        self.assertEqual(len(r['project']['retrieval_decisions']),2)

    async def test_excluded_page_bodies_do_not_enter_returned_background(self):
        r,_=await self.run_records({'formal':{'record_kind':'handoff'},'test':{'record_kind':'test'},'foreign':{}},
               page_metadata={'foreign':{'remote':'github.com/other/example'}})
        self.assertEqual(r['state'],'complete')
        excluded=[s for s in r['sources'] if s['path'].endswith(('-test','-foreign'))]
        self.assertEqual(len(excluded),2)
        # Full evidence is internal/audit-only; default presentation must exclude these bodies.
        from lingxi_connector.context import _fit_output
        from copy import deepcopy
        background = _fit_output(deepcopy(r))
        excluded_paths = {s['path'] for s in excluded}
        self.assertFalse(any(s['path'] in excluded_paths for s in background['sources']))

    async def test_only_test_or_history_is_partial_without_current_fields(self):
        for kind in ('test','history'):
            r,_=await self.run_records({'a':{'record_kind':kind}})
            self.assertEqual(r['state'],'partial')
            self.assertIsNone(r['project']['selected'])
            self.assertEqual(r['progress']['current'],[])

    async def test_unknown_kind_is_not_discarded_by_title_or_formal_priority(self):
        r,_=await self.run_records({'a':{'title':'Example 测试工具项目'}})
        self.assertEqual(r['state'],'complete')
        self.assertEqual(r['project']['retrieval_decisions'][0]['record_kind'],'unknown')
        r,_=await self.run_records({'a':{},'b':{'record_kind':'handoff'}})
        self.assertEqual(r['state'],'partial')
        self.assertIsNone(r['project']['selected'])
        self.assertEqual(r['project']['evidence_gap']['kind'],'replacement_chain')

    async def test_newer_updated_is_not_a_version_tiebreak(self):
        f,hits,pages=fixture({'old':{'record_kind':'handoff'},
                             'new':{'record_kind':'handoff','links':['old']}})
        hits[0].update(updated='2099-01-01T00:00:00Z',score=999)
        pages[hits[0]['path']]['updated']='2099-01-01T00:00:00Z'
        r=await build_context(f,project='Example')
        self.assertEqual(r['state'],'complete')
        self.assertEqual(r['next_step'],'Next new')
        d=next(d for d in r['project']['retrieval_decisions'] if d['decision']=='selected')
        self.assertEqual(d['as_of'],'2026-10-16T12:12:00+08:00')
        self.assertIn('retrieved_at',d)
        self.assertIn('updated',d)
        self.assertFalse(r['consumption']['latest_state_verified'])

    async def test_expired_final_record_never_revives_old(self):
        r,_=await self.run_records({'old':{},'new':{'links':['old'],'valid_until':'2020-01-01T00:00:00Z'}})
        self.assertEqual(r['state'],'partial')
        self.assertIsNone(r['project']['selected'])
        self.assertEqual(r['next_step'],'')

    async def test_invalid_or_future_applicability_cannot_supply_state(self):
        for fields in ({'valid_from':'2099-01-01T00:00:00Z'}, {'valid_until':'2026-10-16'},
                       {'record_kind':'guess'}, {'as_of':'2099-01-01T00:00:00Z'}):
            r,_=await self.run_records({'a':fields})
            self.assertEqual(r['state'],'partial')
            self.assertEqual(r['progress']['current'],[])

    async def test_test_cannot_replace_formal(self):
        r,_=await self.run_records({'old':{'record_kind':'handoff'},'test':{'record_kind':'test','links':['old']}})
        self.assertEqual(r['state'],'conflict')
        self.assertIsNone(r['project']['selected'])

    async def test_filtering_cannot_hide_cross_kind_cycle(self):
        r,_=await self.run_records({'a':{'record_kind':'handoff','links':['b']},
                                   'b':{'record_kind':'test','links':['a']}})
        self.assertEqual(r['state'],'conflict')

    async def test_independent_formal_cannot_hide_fork_in_test_subgraph(self):
        r,_=await self.run_records({'old':{'record_kind':'test'},
            'a':{'record_kind':'test','links':['old']},'b':{'record_kind':'test','links':['old']},
            'formal':{'record_kind':'handoff'}})
        self.assertEqual(r['state'],'conflict')
        self.assertIsNone(r['project']['selected'])

    async def test_same_kind_fork_is_conflict(self):
        r,_=await self.run_records({'old':{},'a':{'record_kind':'handoff','links':['old']},
                                   'b':{'record_kind':'handoff','links':['old']}})
        self.assertEqual(r['state'],'conflict')

    async def test_explicit_foreign_identity_does_not_beat_current_project(self):
        for shape in ('hit_metadata','page_metadata'):
            r,_=await self.run_records({'foreign':{},'own':{'record_kind':'handoff'}},
                  **{shape:{'foreign':{'remote':'github.com/other/example'},
                            'own':{'remote':'github.com/owner/example'}}})
            self.assertEqual(r['state'],'complete')
            self.assertEqual(r['next_step'],'Next own')
            self.assertTrue(any(d['decision']=='foreign_identity' for d in r['project']['retrieval_decisions']))

    async def test_single_foreign_readback_is_never_adopted(self):
        r,_=await self.run_records({'a':{}},page_metadata={'a':{'project':'Other'}})
        self.assertEqual(r['state'],'partial')
        self.assertIsNone(r['project']['selected'])

    async def test_foreign_reference_cannot_be_hidden_by_scope_filter(self):
        r,_=await self.run_records({'foreign':{},'own':{'links':['foreign']}},
               hit_metadata={'foreign':{'remote':'github.com/other/example'}})
        self.assertEqual(r['state'],'project_ambiguous')

    async def test_unrelated_first_batch_uses_remaining_query_and_deduplicates(self):
        _,hits,pages=fixture({'foreign':{},'own':{}})
        hits[0]['front_matter']['remote']='github.com/other/example'
        f=make_fetch(hits=[[hits[0]],[hits[1],hits[1]]],pages=pages)
        r=await build_context(f,project='Example',remote='github.com/owner/example')
        self.assertEqual(r['state'],'complete')
        self.assertEqual(r['next_step'],'Next own')
        self.assertEqual(f.counters['search'],2)
        self.assertEqual([a['path'] for op,a in f.operations if op=='read'].count(hits[1]['path']),1)

    async def test_second_query_overflow_is_partial_without_truncating_candidates(self):
        _,hits,pages=fixture({str(i):{} for i in range(6)})
        foreign=search_hit('wiki/projects/foreign',title='Example',remote='github.com/other/example')
        f=make_fetch(hits=[[foreign],hits],pages=pages)
        r=await build_context(f,project='Example',remote='github.com/owner/example')
        self.assertEqual(r['state'],'partial')
        self.assertEqual(f.counters['search'],2)
        self.assertEqual(r['project']['candidate_count'],6)
        self.assertFalse(any(op=='read' for op,_ in f.operations))

    async def test_descriptive_handoff_candidate_is_prioritized_when_over_window(self):
        # Seven candidates are returned; the newest one has a descriptive handoff title and must be
        # included in the five-candidate read window without turning `updated` into a version pick.
        # The two candidates dropped only because of the read budget stay *known*: the result must
        # therefore stay partial instead of claiming a complete replacement chain.
        records = {f'old{i}': {'record_kind': 'handoff', 'title': 'Example'} for i in range(1, 7)}
        records['new'] = {'record_kind': 'handoff', 'title': 'Example 交接：最新',
                          'links': [f'old{i}' for i in range(1, 5)]}
        _, hits, pages = fixture(records)
        f = make_fetch(hits=hits, pages=pages)
        r = await build_context(f, project='Example', remote='https://github.com/owner/example')
        self.assertEqual(r['project']['candidate_count'], 7)
        self.assertEqual(r['project']['selected'], 'wiki/projects/example-new')
        self.assertEqual(r['state'], 'partial')
        unverified = r['project']['unverified_candidates']
        self.assertEqual(len(unverified), 2)
        self.assertTrue(any('old5' in path or 'old6' in path for path in unverified))
        self.assertTrue(any('未核验的同项目候选' in item for item in r['unknowns']))
        self.assertEqual(r['budget']['search_attempts'], 1)

    async def test_failed_candidate_has_no_retry_or_unsafe_fallback(self):
        _,hits,pages=fixture({'formal':{'record_kind':'handoff'},'test':{'record_kind':'test'}})
        path=hits[0]['path']
        f=make_fetch(hits=hits,pages=pages,fail_reads=(path,))
        r=await build_context(f,project='Example')
        self.assertEqual(r['state'],'partial')
        self.assertIsNone(r['project']['selected'])
        self.assertEqual(sum(op=='read' and a['path']==path for op,a in f.operations),1)

    async def test_scope_and_kind_result_is_invariant_to_score_and_order(self):
        _,hits,pages=fixture({'own':{'record_kind':'handoff'},'test':{'record_kind':'test'},'foreign':{}})
        hits[2]['front_matter']['remote']='github.com/other/example'
        for order in itertools.permutations(hits):
            for score in (0,999):
                hits[2]['score']=score
                r=await build_context(make_fetch(hits=list(order),pages=pages),project='Example',remote='github.com/owner/example')
                self.assertEqual(r['state'],'complete')
                self.assertEqual(r['next_step'],'Next own')
