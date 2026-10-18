import json
import unittest
from pathlib import Path
from unittest.mock import patch

from lingxi_connector.project_identity import (clues, normalize_name, reduce_name, repo_slug,
                                               search_evidence, select_project, strip_credentials)


class NormalizeNameTests(unittest.TestCase):
    def test_folds_width_case_and_separators_to_a_stable_key(self):
        self.assertEqual(normalize_name(' 通用-灵犀 Memory '), '通用灵犀memory')

    def test_full_width_ascii_folds_to_half_width(self):
        self.assertEqual(normalize_name('Ｌｉｎｇｘｉ'), 'lingxi')


class StripCredentialsTests(unittest.TestCase):
    def test_removes_userinfo_and_token_query_from_https(self):
        self.assertEqual(
            strip_credentials('https://user:token@github.com/LDDNYKXO/lingxi-memory-connector.git?token=abc'),
            'github.com/lddnykxo/lingxi-memory-connector')

    def test_normalizes_scp_style_ssh(self):
        self.assertEqual(strip_credentials('git@github.com:LDDNYKXO/lingxi-memory-connector.git'),
                         'github.com/lddnykxo/lingxi-memory-connector')

    def test_normalizes_ssh_url_scheme(self):
        self.assertEqual(strip_credentials('ssh://git@github.com/o/r.git'), 'github.com/o/r')

    def test_unparseable_input_returns_none(self):
        for value in ('not a remote', '', None, 'nodot/path'):
            with self.subTest(value=value):
                self.assertIsNone(strip_credentials(value))

    def test_accepts_a_bare_host_path_identity(self):
        self.assertEqual(strip_credentials('github.com/LDDNYKXO/lingxi-memory-connector'),
                         'github.com/lddnykxo/lingxi-memory-connector')

    def test_bare_host_identity_never_keeps_a_token_or_fragment(self):
        # The skill advertises this bare form, and the first search query is built from the slug:
        # anything after ?/# must never reach the cloud or the reported clue.
        self.assertEqual(strip_credentials('github.com/o/r.git?access_token=secret123'), 'github.com/o/r')
        self.assertEqual(strip_credentials('github.com/o/r#access_token=secret123'), 'github.com/o/r')
        self.assertEqual(strip_credentials('git@github.com:o/r.git?access_token=secret123'), 'github.com/o/r')
        self.assertEqual(repo_slug('github.com/o/r.git?access_token=secret123'), 'r')

    def test_no_credential_survives_the_whole_clue_set(self):
        value = clues(request='继续这个项目', cwd='D:\\o\\r',
                      remote='github.com/o/r.git?access_token=secret123#frag')
        self.assertNotIn('secret123', json.dumps(value, ensure_ascii=False))
        self.assertEqual(value['queries'][0], 'r')


class CluesTests(unittest.TestCase):
    def test_uses_the_project_name_as_the_search_query(self):
        value = clues(request='继续这个项目', project='通用灵犀记忆产品', cwd='D:\\通用灵犀记忆产品')
        self.assertEqual(value['query'], '通用灵犀记忆产品')
        self.assertEqual(value['name_key'], '通用灵犀记忆产品')
        self.assertEqual(value['cwd_key'], 'd:/通用灵犀记忆产品')

    def test_falls_back_to_the_request_when_no_project_name(self):
        value = clues(request='  继续这个项目  ')
        self.assertEqual(value['query'], '继续这个项目')
        self.assertEqual(value['name_key'], '')

    def test_remote_is_kept_only_as_a_credential_free_identity(self):
        value = clues(request='继续', remote='https://user:token@github.com/o/r.git?token=abc')
        self.assertEqual(value['remote_key'], 'github.com/o/r')
        self.assertNotIn('user:token', json.dumps(value, ensure_ascii=False))

    def test_never_scans_directories_or_history(self):
        forbidden = AssertionError('clues must not scan anything')
        with patch('os.walk', side_effect=forbidden), patch('os.scandir', side_effect=forbidden), \
             patch.object(Path, 'iterdir', side_effect=forbidden), \
             patch.object(Path, 'glob', side_effect=forbidden), \
             patch.object(Path, 'rglob', side_effect=forbidden):
            self.assertEqual(clues(request='继续这个项目')['query'], '继续这个项目')

    def test_empty_input_returns_a_safe_empty_key(self):
        self.assertEqual(clues()['query'], '')
        self.assertIsNone(clues()['remote_key'])


def hit(path, title, **meta):
    return {'path': path, 'title': title, 'updated': '2026-10-01T00:00:00+00:00',
            'front_matter': {'title': title, **meta}}


class SelectProjectTests(unittest.TestCase):
    def test_same_name_in_two_different_prefixes_is_ambiguous_not_chosen(self):
        hits = [hit('projects/a/x', '灵犀记忆'), hit('projects/b/x', '灵犀记忆')]
        result = select_project(clues(project='灵犀记忆', cwd='D:\\a'), hits)
        self.assertEqual(result['state'], 'project_ambiguous')
        self.assertEqual(len(result['candidates']), 2)

    def test_same_name_under_one_shared_namespace_is_still_ambiguous(self):
        hits = [hit('wiki/projects/a/index', '灵犀记忆'), hit('wiki/projects/b/index', '灵犀记忆')]
        result = select_project(clues(project='灵犀记忆'), hits)
        self.assertEqual(result['state'], 'project_ambiguous')
        self.assertEqual(len(result['candidates']), 2)

    def test_matches_a_non_git_project_by_directory(self):
        hits = [hit('projects/a/x', 'Other', cwd='D:\\通用灵犀')]
        result = select_project(clues(cwd='d:\\通用灵犀'), hits)
        self.assertEqual(result['state'], 'ok')
        self.assertEqual(result['candidates'][0]['path'], 'projects/a/x')

    def test_matches_by_credential_free_remote(self):
        hits = [hit('projects/a/x', 'Other', remote='git@github.com:o/r.git')]
        result = select_project(clues(remote='https://u:p@github.com/o/r.git?x=1'), hits)
        self.assertEqual(result['state'], 'ok')

    def test_pages_of_one_project_collapse_into_a_single_candidate(self):
        hits = [hit('projects/a/x', '灵犀', project='灵犀记忆'), hit('projects/a/y', '灵犀', project='灵犀记忆')]
        result = select_project(clues(project='灵犀记忆'), hits)
        self.assertEqual(result['state'], 'ok')
        self.assertEqual(len(result['candidates']), 1)

    def test_no_matching_hit_is_none(self):
        self.assertEqual(select_project(clues(project='别的项目'), [hit('projects/a/x', '灵犀记忆')]), {'state': 'none', 'candidates': []})

    def test_empty_clues_never_select(self):
        self.assertEqual(select_project(clues(), [hit('projects/a/x', '灵犀记忆')])['state'], 'none')

    def test_candidate_carries_path_title_updated_and_key(self):
        result = select_project(clues(project='灵犀记忆'), [hit('projects/a/x', '灵犀记忆')])
        self.assertEqual(set(result['candidates'][0]), {'key', 'title', 'path', 'updated'})


class ReduceNameTests(unittest.TestCase):
    def test_drops_generic_product_words_from_both_ends(self):
        self.assertEqual(reduce_name('通用灵犀记忆产品'), '灵犀记忆')

    def test_keeps_a_distinctive_name_untouched(self):
        self.assertEqual(reduce_name('lingxi-connector'), 'lingxi-connector')
        self.assertEqual(reduce_name('灵犀记忆'), '灵犀记忆')

    def test_never_shrinks_below_two_characters(self):
        self.assertEqual(reduce_name('产品'), '产品')
        self.assertEqual(reduce_name('通用'), '通用')

    def test_tolerates_empty_and_non_strings(self):
        self.assertEqual(reduce_name(''), '')
        self.assertEqual(reduce_name(None), '')

    def test_queries_rank_the_reduced_name_before_the_full_one(self):
        value = clues(request='继续这个项目', cwd='D:\\通用灵犀记忆产品')
        self.assertEqual(value['queries'][:2], ['灵犀记忆', '通用灵犀记忆产品'])

    def test_queries_prefer_the_project_name_over_the_directory(self):
        value = clues(request='继续', project='lingxi-connector', cwd='D:\\通用灵犀记忆产品')
        self.assertEqual(value['queries'][0], 'lingxi-connector')

    def test_queries_fall_back_to_the_request_alone(self):
        self.assertEqual(clues(request='继续这个项目')['queries'], ['继续这个项目'])

    def test_query_keeps_its_documented_meaning(self):
        # 兼容字段：仍是“项目名优先，否则请求文本”，供展示与旧调用方使用。
        value = clues(request='继续这个项目', project='通用灵犀记忆产品', cwd='D:\\通用灵犀记忆产品')
        self.assertEqual(value['query'], '通用灵犀记忆产品')


class RepositorySlugTests(unittest.TestCase):
    def test_slug_comes_from_a_credential_free_remote(self):
        self.assertEqual(repo_slug('https://user:token@github.com/LDDNYKXO/lingxi-memory-connector.git'),
                         'lingxi-memory-connector')

    def test_slug_accepts_owner_repo_shorthand(self):
        self.assertEqual(repo_slug('LDDNYKXO/lingxi-memory-connector'), 'lingxi-memory-connector')

    def test_slug_is_empty_without_a_remote(self):
        self.assertEqual(repo_slug(''), '')
        self.assertEqual(repo_slug(None), '')

    def test_queries_put_the_repository_slug_first_when_available(self):
        value = clues(request='继续这个项目', cwd='D:\\通用灵犀记忆产品',
                      remote='https://github.com/LDDNYKXO/lingxi-memory-connector.git')
        self.assertEqual(value['queries'][0], 'lingxi-memory-connector')
        self.assertEqual(value['remote_key'], 'github.com/lddnykxo/lingxi-memory-connector')


class SearchEvidenceTests(unittest.TestCase):
    def clue_set(self, **overrides):
        values = {'project': '', 'cwd': 'D:\\通用灵犀记忆产品', 'request': '继续这个项目',
                  'remote_key': 'github.com/lddnykxo/lingxi-memory-connector'}
        values.update(overrides)
        return clues(request=values['request'], project=values['project'], cwd=values['cwd'],
                     remote=values['remote_key'])

    def row(self, path, score):
        return {'path': path, 'title': ('lingxi-memory-connector' if '29927ba0' in path else path), 'summary': None, 'snippet': None, 'score': score,
                'updated': 1}

    def test_repository_slug_query_may_select_the_top_project_page(self):
        rows = [self.row('wiki/projects/lingxi-connector-29927ba0', 79.4),
                self.row('wiki/entities/dsh-cost-meter', 59.0)]
        result = search_evidence(self.clue_set(), 'lingxi-memory-connector', rows)
        self.assertEqual(result['state'], 'ok')
        self.assertEqual(result['evidence'], 'identifier_match')
        self.assertEqual(result['candidates'][0]['path'], 'wiki/projects/lingxi-connector-29927ba0')

    def test_non_discriminating_query_never_uses_the_evidence_channel(self):
        rows = [self.row('wiki/projects/lingxi-connector-29927ba0', 79.4)]
        self.assertEqual(search_evidence(self.clue_set(), '继续这个项目', rows)['state'], 'none')
        self.assertEqual(search_evidence(self.clue_set(), 'first-round', rows)['state'], 'none')

    def test_an_explicit_project_name_counts_as_discriminating(self):
        rows = [{**self.row('wiki/projects/anything', 10.0), 'title': '灵犀记忆当前阶段'}]
        result = search_evidence(self.clue_set(project='灵犀记忆', remote_key=None), '灵犀记忆', rows)
        self.assertEqual(result['state'], 'ok')

    def test_hits_outside_the_project_namespace_are_not_evidence(self):
        rows = [self.row('wiki/concepts/lingxi-connector-check', 90.0)]
        self.assertEqual(search_evidence(self.clue_set(), 'lingxi-memory-connector', rows)['state'], 'none')

    def test_runner_up_project_pages_are_reported_not_chosen(self):
        rows = [self.row('wiki/projects/lingxi-connector-29927ba0', 79.4),
                self.row('wiki/projects/灵犀原生MCP验收', 58.7)]
        result = search_evidence(self.clue_set(), 'lingxi-memory-connector', rows)
        self.assertEqual(result['candidates'][0]['path'], 'wiki/projects/lingxi-connector-29927ba0')
        self.assertEqual(result['runner_ups'], ['wiki/projects/灵犀原生MCP验收'])


if __name__ == '__main__':
    unittest.main()
