import json
import unittest
from lingxi_connector.context import build_context
from lingxi_connector.project_identity import clues, search_evidence
from test_context import search_hit
from lingxi_connector.project_record import parse_record
from test_context import make_fetch, page
from lingxi_connector.context import _build_context as build_context  # Raw retrieval; layering tested separately.



class IdentityCompatibilityTests(unittest.TestCase):
    def test_combined_names_are_searchable_terms_not_one_concatenated_identity(self):
        c = clues(project='Example 项目 / example-repo', cwd='C:/default')
        self.assertEqual(c['queries'][:2], ['Example', 'example-repo'])
        hit = {'path': 'wiki/projects/example', 'title': 'Example current'}
        self.assertEqual(search_evidence(c, 'Example', [hit])['state'], 'ok')

    def test_lx_name_can_find_legacy_page_within_two_queries(self):
        c = clues(project='LX Memory')
        self.assertEqual(c['queries'][:2], ['LX Memory', 'lingxi-memory-connector'])
        hit = {'path': 'wiki/projects/legacy', 'title': '灵犀记忆 MCP（lingxi-memory-connector）：当前阶段'}
        self.assertEqual(search_evidence(c, 'lingxi-memory-connector', [hit])['state'], 'ok')

    def test_unrelated_projects_do_not_inherit_lx_aliases(self):
        self.assertNotIn('LX Memory', clues(project='Another project')['queries'])

    def test_renamed_remote_preserves_legacy_discovery_and_record_identity(self):
        c = clues(remote='https://github.com/LDDNYKXO/LX-Memory.git')
        self.assertEqual(c['queries'][:2], ['lx-memory', 'lingxi-memory-connector'])
        self.assertIn('lingximemoryconnector', c['name_keys'])
        self.assertEqual(search_evidence(c, 'lx-memory', [
            {'path': 'wiki/projects/lx', 'title': 'LX Memory current'}])['state'], 'ok')

    def test_foreign_owner_does_not_inherit_this_repositories_old_names(self):
        c = clues(remote='https://github.com/SomeoneElse/LX-Memory.git')
        self.assertNotIn('lingxi-memory-connector', c['queries'])
        self.assertNotIn('灵犀记忆 MCP', c['queries'])


def record(project, supersedes):
    return '<!-- memory-project-record ' + json.dumps({
        'schema': 1, 'project': project, 'supersedes': supersedes,
        'as_of': '2026-10-16T08:00:00+08:00'}, ensure_ascii=False) + ' -->\n'


class ContinuityRecordTests(unittest.IsolatedAsyncioTestCase):
    def test_record_rejects_unsafe_paths_boolean_schema_and_naive_time(self):
        good = {'schema': 1, 'project': 'Example', 'supersedes': ['wiki/projects/old'],
                'as_of': '2026-10-16T08:00:00+08:00'}
        for change in ({'schema': True}, {'supersedes': ['wiki/projects/../secret']},
                       {'supersedes': ['wiki/projects/old?token=x']},
                       {'as_of': '2026-10-16T08:00:00'}):
            with self.subTest(change=change):
                text = '<!-- memory-project-record ' + json.dumps({**good, **change}) + ' -->'
                self.assertIsNone(parse_record(text, ['example']))

    def fixture(self, marker):
        old, new = 'wiki/projects/example-old', 'wiki/projects/example-new'
        hits = [{'path': old, 'title': 'Example old'}, {'path': new, 'title': 'Example current'}]
        pages = {old: page(old, content='### 目标\nOld\n### 当前状态\nOld\n### 下一步\nOld'),
                 new: page(new, content=marker + '### 目标\nCurrent goal\n### 当前状态\nCurrent state\n### 下一步\nCurrent next')}
        return make_fetch(hits=hits, pages=pages)

    async def test_explicit_same_project_supersession_selects_current_record(self):
        f = self.fixture(record('Example', ['wiki/projects/example-old']))
        r = await build_context(f, project='Example', request='Continue')
        self.assertEqual(r['state'], 'complete')
        self.assertEqual(r['project']['selected'], 'wiki/projects/example-new')
        self.assertEqual(r['goal'], 'Current goal')
        self.assertEqual(r['record']['as_of'], '2026-10-16T08:00:00+08:00')
        self.assertEqual(r['record']['supersedes'], ['wiki/projects/example-old'])
        self.assertLessEqual(len([op for op, _ in f.operations if op == 'read']), 3)

    async def test_another_projects_claim_cannot_resolve_ambiguity(self):
        r = await build_context(self.fixture(record('Other', ['wiki/projects/example-old'])), project='Example')
        self.assertEqual(r['state'], 'project_ambiguous')

    async def test_arbitrary_prose_or_fenced_marker_does_not_select_a_winner(self):
        for text in ['This page replaces the old record.', '```\n' + record('Example', ['wiki/projects/example-old']) + '```']:
            r = await build_context(self.fixture(text), project='Example')
            self.assertEqual(r['state'], 'project_ambiguous')

    async def test_context_declares_source_boundary_on_partial_results(self):
        r = await build_context(make_fetch(), project='Example')
        self.assertFalse(r['consumption']['filesystem_background_fallback'])
        self.assertFalse(r['consumption']['extra_search_fallback'])
        self.assertFalse(r['consumption']['latest_state_verified'])

    async def test_explicit_replacement_cannot_cross_conflicting_repository_evidence(self):
        old, new = 'wiki/projects/example-old', 'wiki/projects/example-new'
        f = make_fetch(hits=[search_hit(old, project='Example', remote='github.com/other/example'),
                             search_hit(new, project='Example', remote='github.com/owner/example')],
                       pages={old: page(old, remote='github.com/other/example'),
                              new: page(new, remote='github.com/owner/example',
                                        content=record('Example', [old]) + '### 目标\nG\n### 当前状态\nS\n### 下一步\nN')})
        r = await build_context(f, project='Example')
        self.assertEqual(r['state'], 'project_ambiguous')

    async def test_top_level_identity_is_not_hidden_by_partial_frontmatter(self):
        old, new = 'wiki/projects/example-old', 'wiki/projects/example-new'
        hits = [dict(search_hit(old, project='Example'), remote='github.com/other/example'),
                dict(search_hit(new, project='Example'), remote='github.com/owner/example')]
        f = make_fetch(hits=hits, pages={old: page(old), new: page(new,
            content=record('Example', [old]) + '### 目标\nG\n### 当前状态\nS\n### 下一步\nN')})
        r = await build_context(f, project='Example', remote='github.com/owner/example')
        self.assertEqual(r['state'], 'project_ambiguous')
