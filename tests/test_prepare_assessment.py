"""Synthetic offline project-preparation contract; never reads real user memory."""
import asyncio
import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from lingxi_connector.cloud import ConnectorError
from lingxi_connector.prepare_assessment import assess
from lingxi_connector.writeback import Writeback, digest
from test_writeback import FakeSession

NOW = datetime(2026, 10, 16, 10, tzinfo=timezone.utc)


def body(project='Example', supersedes=(), as_of='2026-10-16T08:00:00+00:00', **extra):
    marker = dict(schema=1, project=project, supersedes=list(supersedes), as_of=as_of,
                  record_kind='handoff', **extra)
    return '<!-- memory-project-record ' + json.dumps(marker, ensure_ascii=False) + ' -->\n### 目标\nG\n### 当前状态\nS\n### 下一步\nN'


def page(path='wiki/projects/old', content=None, **extra):
    return dict(path=path, content=body() if content is None else content,
                front_matter={'title': 'Title', 'summary': 'Summary'}, source='lingxi_cloud',
                content_scope='full', truncated=False, updated=123, **extra)


def evaluate(content=None, pages=()):
    return assess('projects', 'Title', 'Summary', body() if content is None else content,
                  list(pages), now=NOW)


class AssessmentTests(unittest.TestCase):
    def test_exact_match_not_title_alone(self):
        r = evaluate(pages=[page()])
        self.assertEqual(r['candidates'][0]['classification'], 'exact_duplicate')
        self.assertEqual(r['state'], 'checked')

    def test_only_line_endings_are_normalized(self):
        self.assertEqual(evaluate(body().replace('\n', '\r\n'), [page()])['candidates'][0]['classification'], 'exact_duplicate')
        self.assertEqual(evaluate(body() + ' ', [page()])['candidates'][0]['classification'], 'related_changed')

    def test_metadata_changes_are_not_exact_duplicates(self):
        p = page(); p['front_matter']['summary'] = 'Different'
        self.assertEqual(evaluate(pages=[p])['candidates'][0]['classification'], 'related_changed')

    def test_same_title_other_project_is_excluded(self):
        r = evaluate(pages=[page(content=body(project='Other'))])
        self.assertEqual(r['candidates'][0]['classification'], 'foreign_project')
        self.assertFalse(r['needs_confirmation'])

    def test_legacy_scope_is_unknown(self):
        r = evaluate(pages=[page(content='Unmarked old prose')])
        self.assertEqual(r['state'], 'partial')
        self.assertTrue(r['needs_confirmation'])

    def test_draft_requires_explicit_project_identity(self):
        self.assertEqual(evaluate('Unmarked draft')['state'], 'partial')

    def test_partial_page_cannot_prove_exact_duplicate(self):
        p = page(); p['truncated'] = True; p['content_scope'] = 'partial'
        r = evaluate(pages=[p])
        self.assertEqual(r['state'], 'partial')
        self.assertNotEqual(r['candidates'][0]['classification'], 'exact_duplicate')

    def test_explicit_new_handoff_preserves_old_version(self):
        r = evaluate(body(supersedes=['wiki/projects/old']), [page()])
        self.assertEqual(r['version_resolution']['selected'], 'proposed_draft')
        self.assertEqual(r['candidates'][0]['classification'], 'explicitly_superseded')

    def test_unread_replacement_is_not_followed_or_proven(self):
        self.assertEqual(evaluate(body(supersedes=['wiki/projects/missing']))['state'], 'partial')

    def test_only_explicit_comparable_fact_change_is_potential_conflict(self):
        r = evaluate(body(facts={'runtime': 'new'}), [page(content=body(facts={'runtime': 'old'}))])
        self.assertEqual(r['candidates'][0]['classification'], 'potential_conflict')
        self.assertTrue(r['needs_confirmation'])
        self.assertEqual(r['candidates'][0]['conflict_fields'], ['runtime'])

    def test_different_as_of_without_interval_is_not_automatic_conflict(self):
        r = evaluate(body(facts={'runtime': 'new'}), [page(content=body(as_of='2026-10-15T08:00:00+00:00', facts={'runtime': 'old'}))])
        self.assertEqual(r['candidates'][0]['classification'], 'related_changed')

    def test_disjoint_or_historical_facts_do_not_conflict(self):
        old = body(facts={'runtime': 'old'}, valid_until='2026-10-16T07:00:00+00:00')
        r = evaluate(body(facts={'runtime': 'new'}, valid_from='2026-10-16T07:00:00+00:00'), [page(content=old)])
        self.assertNotEqual(r['candidates'][0]['classification'], 'potential_conflict')

    def test_future_draft_and_malformed_policy_need_review(self):
        for b in (body(as_of='2030-01-01T00:00:00+00:00'), body(valid_until='not-a-date')):
            self.assertTrue(evaluate(b)['needs_confirmation'])

    def test_cycle_is_not_hidden(self):
        a = page('wiki/projects/a', body(supersedes=['wiki/projects/b']))
        b = page('wiki/projects/b', body(supersedes=['wiki/projects/a']))
        r = evaluate(pages=[a, b])
        self.assertEqual(r['state'], 'conflict')

    def test_inconsistent_page_metadata_is_not_same_scope(self):
        p = page(); p['front_matter']['project'] = 'Other'
        self.assertEqual(evaluate(pages=[p])['candidates'][0]['classification'], 'foreign_project')

    def test_no_candidate_is_only_limited_check(self):
        r = evaluate()
        self.assertEqual(r['state'], 'checked')
        self.assertEqual(r['coverage'], 'returned_candidates_only')
        self.assertFalse(r['global_dedup_verified'])

    def test_invalid_draft_interval_never_enters_fact_comparison(self):
        r = evaluate(body(facts={'runtime': 'new'}, valid_from='invalid', valid_until='invalid'),
            [page(content=body(as_of='2026-10-15T08:00:00+00:00', facts={'runtime': 'old'},
                               valid_from='2026-10-15T00:00:00+00:00', valid_until='2026-10-17T00:00:00+00:00'))])
        self.assertEqual(r['state'], 'partial')

    def test_explicit_overlapping_intervals_can_signal_possible_conflict(self):
        r = evaluate(body(facts={'runtime': 'new'}, valid_from='2026-10-16T00:00:00+00:00',
                          valid_until='2026-10-18T00:00:00+00:00'),
            [page(content=body(as_of='2026-10-15T08:00:00+00:00', facts={'runtime': 'old'},
                               valid_from='2026-10-15T00:00:00+00:00', valid_until='2026-10-17T00:00:00+00:00'))])
        self.assertEqual(r['candidates'][0]['classification'], 'potential_conflict')

    def test_too_many_supplied_candidates_is_partial(self):
        r = evaluate(pages=[page('wiki/projects/' + str(i)) for i in range(4)])
        self.assertEqual(r['state'], 'partial')

    def test_structural_fact_scope_is_project_and_not_free_prose(self):
        r = evaluate(body() + '\nThe runtime is new', [page(content=body() + '\nThe runtime is old')])
        self.assertEqual(r['candidates'][0]['classification'], 'related_changed')


class AssessmentSession(FakeSession):
    def __init__(self, pages=()):
        self.pages = list(pages)
        self.calls = []
        self.fail_search = False
        self.change_account = False
    async def search(self, query, limit):
        self.calls.append(('search', query, limit))
        if self.fail_search: raise ConnectorError('NETWORK_ERROR', 'must not leak secrets')
        return {'source': 'lingxi_cloud', 'total': len(self.pages), 'results': [{'path': p['path']} for p in self.pages]}
    async def read(self, path, offset=0, max_chars=20000):
        self.calls.append(('read', path, max_chars))
        for p in self.pages:
            if p['path'] == path:
                if self.change_account: self.account = 'different-account'
                out = copy.deepcopy(p)
                if len(out['content']) > max_chars:
                    out.update(content=out['content'][:max_chars], content_scope='partial', truncated=True)
                return out
        return await super().read(path, offset, max_chars)


class AssessmentIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = AssessmentSession()
        self.w = Writeback(self.session, Path(self.tmp.name))
    async def asyncTearDown(self): self.tmp.cleanup()
    async def prepare(self, **kwargs):
        return await self.w.prepare('projects', 'Title', 'Summary', body(), **kwargs)

    async def test_all_three_candidates_including_nonfirst_duplicate(self):
        self.session.pages = [page('wiki/projects/1', body(project='Other')),
                              page('wiki/projects/2', body() + ' '), page('wiki/projects/3')]
        p = await self.prepare()
        self.assertEqual([r['classification'] for r in p['assessment']['candidates']],
                         ['foreign_project', 'related_changed', 'exact_duplicate'])
        self.assertEqual(len([c for c in self.session.calls if c[0] == 'search']), 1)
        self.assertEqual(self.session.posts, 0)
        self.assertEqual(p['preview']['content'], body())
        self.assertEqual(p['sha256'], digest(p['preview']))

    async def test_partial_assessment_blocks_commit_until_new_reviewed_preview(self):
        self.session.pages = [page(content='Unknown legacy scope')]
        p = await self.prepare()
        self.assertEqual((await self.w.commit(p['proposal_id']))['error'], 'ASSESSMENT_REVIEW_REQUIRED')
        self.assertEqual(self.session.posts, 0)
        q = await self.prepare(assessment_confirmation=p['assessment_review_key'])
        self.assertTrue(q['assessment_reviewed'])
        self.assertNotEqual(p['proposal_id'], q['proposal_id'])
        self.assertEqual(q['preview']['content'], p['preview']['content'])
        self.assertEqual((await self.w.commit(q['proposal_id']))['state'], 'saved_verified')

    async def test_confirmation_is_bound_to_content_and_new_evidence(self):
        self.session.pages = [page(content='Unknown legacy scope')]
        p = await self.prepare()
        self.session.pages[0]['updated'] = 456
        q = await self.prepare(assessment_confirmation=p['assessment_review_key'])
        self.assertFalse(q['assessment_reviewed'])
        q = await self.w.prepare('projects', 'Title', 'Summary', body() + ' ', assessment_confirmation=p['assessment_review_key'])
        self.assertFalse(q['assessment_reviewed'])

    async def test_failed_search_is_partial_not_no_duplicate(self):
        self.session.fail_search = True
        p = await self.prepare()
        self.assertEqual(p['assessment']['state'], 'partial')
        self.assertNotIn('must not leak', json.dumps(p))
        self.assertEqual(self.session.posts, 0)

    async def test_account_change_during_assessment_rejects_preview(self):
        self.session.pages = [page()]; self.session.change_account = True
        with self.assertRaises(ConnectorError) as error: await self.prepare()
        self.assertEqual(error.exception.code, 'ACCOUNT_CHANGED')
        self.assertEqual(self.w.proposals, {})

    async def test_budget_is_bounded_and_incomplete_page_never_exact(self):
        self.session.pages = [page('wiki/projects/' + str(i), body() + 'X' * 15000) for i in range(4)]
        p = await self.prepare()
        reads = [c for c in self.session.calls if c[0] == 'read']
        self.assertLessEqual(len(reads), 3)
        self.assertLessEqual(sum(c[2] for c in reads), 12000)
        self.assertEqual(p['assessment']['state'], 'partial')

    async def test_unknown_outcome_is_not_resubmitted(self):
        p = await self.prepare(); self.session.fail_post = True
        self.assertEqual((await self.w.commit(p['proposal_id']))['state'], 'unknown')
        self.assertEqual((await self.w.commit(p['proposal_id']))['state'], 'unknown')
        self.assertEqual(self.session.posts, 1)

    async def test_payload_mutation_is_rejected_before_post(self):
        p = await self.prepare()
        self.w.proposals[p['proposal_id']]['payload']['content'] = 'mutated'
        self.assertEqual((await self.w.commit(p['proposal_id']))['error'], 'PREVIEW_CHANGED')
        self.assertEqual(self.session.posts, 0)

    async def test_timeout_is_finite_and_reported(self):
        with patch('lingxi_connector.writeback.ASSESSMENT_TIMEOUT', 0.01):
            async def slow(*args): await asyncio.sleep(1)
            self.session.search = slow
            p = await self.prepare()
        self.assertEqual(p['assessment']['state'], 'partial')

    async def test_candidate_front_matter_change_invalidates_review(self):
        self.session.pages = [page(content='Unknown legacy scope')]
        p = await self.prepare()
        self.session.pages[0]['front_matter']['summary'] = 'New evidence'
        q = await self.prepare(assessment_confirmation=p['assessment_review_key'])
        self.assertFalse(q['assessment_reviewed'])

    async def test_returned_preview_and_assessment_are_detached(self):
        self.session.pages = [page(content='Unknown legacy scope')]
        p = await self.prepare()
        p['assessment']['needs_confirmation'] = False
        p['preview']['content'] = 'mutated caller copy'
        self.assertTrue(self.w.proposals[p['proposal_id']]['assessment']['needs_confirmation'])
        self.assertEqual(self.w.proposals[p['proposal_id']]['payload']['content'], body())

    async def test_same_account_epoch_change_in_second_identity_is_rejected(self):
        calls = 0
        async def identity():
            nonlocal calls
            calls += 1
            if calls == 2: self.session.epoch += 1
            return self.session.account
        self.session.identity = identity
        with self.assertRaises(ConnectorError): await self.prepare()

    async def test_concurrent_project_commit_is_still_at_most_once(self):
        p = await self.prepare()
        results = await asyncio.gather(*(self.w.commit(p['proposal_id']) for _ in range(3)))
        self.assertTrue(all(r['state'] == 'saved_verified' for r in results))
        self.assertEqual(self.session.posts, 1)

    async def test_invalid_or_nonproject_candidate_path_is_never_read(self):
        self.session.pages = [page('wiki/projects/../private'), page('core/核心记忆')]
        p = await self.prepare()
        self.assertFalse(any(c[0] == 'read' for c in self.session.calls))
        self.assertEqual(p['assessment']['state'], 'partial')

    async def test_unreturned_search_results_require_review_without_extra_search(self):
        self.session.pages = [page('wiki/projects/' + str(i)) for i in range(3)]
        original = self.session.search
        async def paged(*args): return {**await original(*args), 'total': 4}
        self.session.search = paged
        p = await self.prepare()
        self.assertEqual(p['assessment']['state'], 'partial')
        self.assertEqual(p['blocked_reason'], 'ASSESSMENT_REVIEW_REQUIRED')
        self.assertEqual((await self.w.commit(p['proposal_id']))['error'], 'ASSESSMENT_REVIEW_REQUIRED')
        self.assertEqual(sum(c[0] == 'search' for c in self.session.calls), 1)
        self.assertEqual(self.session.posts, 0)

    async def test_invalid_search_total_is_not_a_complete_check(self):
        original = self.session.search
        for total in (-1, True, '3'):
            async def malformed(*args): return {**await original(*args), 'total': total}
            self.session.search = malformed
            p = await self.prepare()
            self.assertEqual(p['assessment']['state'], 'partial')


class AssessmentMcpContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_required_arguments_are_compatible(self):
        from lingxi_connector.mcp_server import create_server
        tools = {t.name: t for t in await create_server(Path('.')).list_tools()}
        schema = tools['lingxi_memory_prepare'].input_schema
        self.assertEqual(set(schema['required']), {'category', 'title', 'summary', 'content'})
        self.assertIn('assessment_confirmation', schema['properties'])

    async def test_optional_review_key_is_forwarded_only_when_supplied(self):
        from lingxi_connector.mcp_server import create_server
        original = dict(category='projects', title='Title', summary='Summary', content=body())
        for extra in ({}, {'assessment_confirmation': 'a' * 64}):
            with patch('lingxi_connector.mcp_server.call', return_value={'preview': {}}) as called:
                await create_server(Path('.')).call_tool('lingxi_memory_prepare', {**original, **extra})
            self.assertEqual(called.call_args.args[1], 'prepare')
            self.assertEqual(called.call_args.args[2], {**original, **extra})

