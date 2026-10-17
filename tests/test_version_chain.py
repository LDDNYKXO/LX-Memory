"""Synthetic-only version graphs. No cloud, browser, native host or filesystem memory."""
import itertools
import json
import unittest
from unittest.mock import patch
from lingxi_connector.context import build_context, OUTPUT_MAX_CHARS
from test_context import make_fetch, page, search_hit
from test_lx_memory import record
from lingxi_connector.context import _build_context as build_context  # Raw retrieval; layering tested separately.



class VersionChainTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, links, markers=None, order=None, page_meta=None, hit_meta=None, **kwargs):
        paths={name:'wiki/projects/example-'+name for name in links}
        pages={}
        hits=[]
        for name in order or list(links):
            path=paths[name]
            hit=search_hit(path,title='Example '+name,**(hit_meta or {}).get(name,{}))
            hits.append(hit)
            marker=record((markers or {}).get(name,'Example'),
                          [paths.get(target,target) for target in links[name]])
            pages[path]=page(path,content=marker+'### 目标\nGoal '+name+
                '\n### 当前状态\nState '+name+'\n### 下一步\nNext '+name,
                **(page_meta or {}).get(name,{}))
        return make_fetch(hits=hits,pages=pages,**kwargs),paths

    async def run_graph(self, links, **kwargs):
        f,paths=self.fixture(links,**kwargs)
        r=await build_context(f,project='Example',request='Continue')
        self.assertTrue(all(op in ('status','search','read') for op,_ in f.operations))
        return r,f,paths

    async def test_three_direct_replacements_select_unique_winner(self):
        r,f,p=await self.run_graph({'old':[],'middle':['old'],'new':['old','middle']})
        self.assertEqual(r['state'],'complete')
        self.assertEqual(r['project']['selected'],p['new'])
        self.assertEqual(r['goal'],'Goal new')
        self.assertEqual(len([1 for op,_ in f.operations if op=='read']),4)

    async def test_transitive_chain_is_order_independent_and_does_not_inherit_old_fields(self):
        for order in itertools.permutations(['old','middle','new']):
            r,_,p=await self.run_graph({'old':[],'middle':['old'],'new':['middle']},order=order)
            self.assertEqual(r['state'],'complete')
            self.assertEqual(r['project']['selected'],p['new'])
            self.assertEqual(r['progress']['current'],['State new'])
            self.assertEqual(r['next_step'],'Next new')

    async def test_five_versions_stay_inside_bounded_reads(self):
        r,f,p=await self.run_graph({'a':[],'b':['a'],'c':['b'],'d':['c'],'e':['d']})
        self.assertEqual(r['state'],'complete')
        self.assertEqual(r['project']['selected'],p['e'])
        self.assertLessEqual(len([1 for op,_ in f.operations if op=='read']),6)

    async def test_shared_repository_or_directory_does_not_hide_version_paths(self):
        for field,value in (('remote','github.com/owner/example'),('cwd','D:/Example')):
            meta={n:{'project':'Example',field:value} for n in ('old','middle','new')}
            r,_,p=await self.run_graph({'old':[],'middle':['old'],'new':['middle']},hit_meta=meta)
            self.assertEqual(r['state'],'complete')
            self.assertEqual(r['project']['selected'],p['new'])

    async def test_shared_identity_does_not_hide_cycles_or_forks(self):
        meta={n:{'project':'Example','remote':'github.com/owner/example'} for n in ('old','middle','new')}
        for links in ({'old':['middle'],'middle':['old'],'new':['middle']},
                      {'old':[],'middle':['old'],'new':['old']}):
            r,_,_=await self.run_graph(links,hit_meta=meta)
            self.assertEqual(r['state'],'conflict')
            self.assertIsNone(r['project']['selected'])

    async def test_mixed_identity_metadata_keeps_identifier_matched_versions(self):
        graphs = [({'old':[],'middle':['old'],'new':['middle']}, 'complete'),
                  ({'old':['middle'],'middle':['old'],'new':['middle']}, 'conflict'),
                  ({'old':[],'middle':['old'],'new':['old']}, 'conflict')]
        for links, expected in graphs:
            for exact in ('old', 'middle', 'new'):
                r,_,p=await self.run_graph(links,hit_meta={exact:{
                    'project':'Example','remote':'github.com/owner/example'}})
                self.assertEqual(r['state'],expected)
                self.assertEqual(r['project']['selected'],p['new'] if expected=='complete' else None)

    async def test_cycle_is_conflict_even_with_a_newer_root_claim(self):
        for links in ({'a':['b'],'b':['a'],'c':['a','b']}, {'a':['a'],'b':['a'],'c':['b']}):
            r,_,_=await self.run_graph(links)
            self.assertEqual(r['state'],'conflict')
            self.assertIsNone(r['project']['selected'])
            self.assertTrue(r['conflicts'])

    async def test_competing_replacements_are_conflict_not_score_or_date_tiebreak(self):
        r,_,_=await self.run_graph({'old':[],'a':['old'],'b':['old']})
        self.assertEqual(r['state'],'conflict')
        self.assertIsNone(r['project']['selected'])

    async def test_unlinked_same_project_candidate_reports_version_evidence_gap(self):
        r,_,_=await self.run_graph({'old':[],'new':['old'],'unknown':[]})
        self.assertEqual(r['state'],'partial')
        self.assertEqual(r['project']['evidence_gap']['kind'],'replacement_chain')

    async def test_foreign_repository_cannot_be_superseded(self):
        for metadata in ('page_meta','hit_meta'):
            r,_,_=await self.run_graph({'old':[],'middle':['old'],'new':['middle']},
                 **{metadata:{'old':{'remote':'github.com/other/example'},
                              'new':{'remote':'github.com/owner/example'}}})
            self.assertEqual(r['state'],'project_ambiguous')
            self.assertIsNone(r['project']['selected'])

    async def test_foreign_project_marker_cannot_be_used_as_an_intermediate(self):
        r,_,_=await self.run_graph({'old':[],'middle':['old'],'new':['middle','old']},
                                   markers={'middle':'Other'})
        self.assertEqual(r['state'],'project_ambiguous')

    async def test_readback_project_metadata_cannot_be_overridden_by_marker(self):
        r,_,_=await self.run_graph({'old':[],'middle':['old'],'new':['middle','old']},
                                   page_meta={'old':{'project':'Other'}})
        self.assertEqual(r['state'],'project_ambiguous')

    async def test_partial_scope_without_truncated_flag_is_not_a_complete_chain(self):
        f,p=self.fixture({'old':[],'middle':['old'],'new':['middle']})
        async def fetch(op,args):
            result=await f(op,args)
            if op=='read' and args['path']==p['old']:
                result={**result,'content_scope':'partial','truncated':False}
            return result
        r=await build_context(fetch,project='Example')
        self.assertEqual(r['state'],'partial')
        self.assertIsNone(r['project']['selected'])

    async def test_explicit_unreturned_link_gets_one_bounded_verification_attempt(self):
        missing='wiki/projects/not-in-search'
        r,f,_=await self.run_graph({'old':[],'middle':['old'],'new':[missing]})
        self.assertNotEqual(r['state'],'complete')
        self.assertEqual(sum(args.get('path')==missing for _,args in f.operations),1)
        self.assertEqual(r['state'],'partial')

    async def test_character_budget_exhaustion_returns_partial_without_a_winner(self):
        f,_=self.fixture({'old':[],'middle':['old'],'new':['middle']})
        with patch('lingxi_connector.context.CONTEXT_TOTAL_CHARS',30):
            r=await build_context(f,project='Example')
        self.assertEqual(r['state'],'partial')
        self.assertIsNone(r['project']['selected'])
        self.assertLessEqual(len([1 for op,_ in f.operations if op=='read']),1)

    async def test_time_budget_exhaustion_returns_partial_without_a_winner(self):
        f,_=self.fixture({'old':[],'middle':['old'],'new':['middle']})
        clock=iter([0,46,46,46,46,46])
        r=await build_context(f,project='Example',now=lambda:next(clock))
        self.assertEqual(r['state'],'partial')
        self.assertIsNone(r['project']['selected'])
        self.assertFalse(any(op=='read' for op,_ in f.operations))

    async def test_failed_candidate_read_is_partial_and_not_retried(self):
        r,f,_=await self.run_graph({'old':[],'middle':['old'],'new':['middle']},
                                   fail_reads=('wiki/projects/example-middle',))
        self.assertEqual(r['state'],'partial')
        self.assertIsNone(r['project']['selected'])
        self.assertEqual(len([1 for op,args in f.operations if args.get('path')=='wiki/projects/example-middle']),1)

    async def test_candidate_overflow_is_partial_without_unbounded_reads(self):
        r,f,_=await self.run_graph({str(i):[] for i in range(6)})
        self.assertEqual(r['state'],'partial')
        self.assertIsNone(r['project']['selected'])
        self.assertFalse(any(op=='read' for op,_ in f.operations))

    async def test_test_record_cycle_cannot_be_hidden_by_kind_filtering(self):
        f,p=self.fixture({'a':['b'],'b':['a'],'formal':[]})
        async def fetch(op,args):
            result=await f(op,args)
            if op=='read' and args['path'] in (p['a'],p['b']):
                result={**result,'content':result['content'].replace('"schema": 1','"record_kind": "test", "schema": 1')}
                # The older helper serializes compact JSON, too.
                if '"record_kind"' not in result['content']:
                    result['content']=result['content'].replace('"schema":1','"record_kind":"test","schema":1')
            return result
        r=await build_context(fetch,project='Example')
        self.assertEqual(r['state'],'conflict')
        self.assertIsNone(r['project']['selected'])
