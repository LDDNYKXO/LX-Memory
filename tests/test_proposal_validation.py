"""Read-only pre-commit validation tests (synthetic session; no cloud calls)."""
import unittest

from lingxi_connector.cloud import ConnectorError
from lingxi_connector.proposal_validation import validate_proposal
from test_record_policy import body


def handoff(supersedes=(), as_of='2026-10-17T12:00:00+08:00', project='Example'):
    return (body(project=project, supersedes=list(supersedes), as_of=as_of, record_kind='handoff')
            + '### 项目身份与目标\nGoal\n### 当前状态\nState\n### 下一步\nNext\n')


def hit(path, title='Example', project='Example'):
    return {'path': path, 'title': title, 'summary': None, 'score': 1.0,
            'updated': '2026-10-17T12:00:00+08:00',
            'front_matter': {'title': title, 'project': project}}


def page(path, content, title='Example'):
    return {'path': path, 'front_matter': {'title': title, 'project': 'Example'},
            'content': content, 'updated': '2026-10-17T12:00:00+08:00',
            'retrieved_at': '2026-10-17T12:01:00+08:00', 'source': 'lingxi_cloud',
            'content_total_chars': len(content), 'offset': 0, 'truncated': False,
            'next_offset': None, 'content_scope': 'full'}


class FakeSession:
    def __init__(self, hits, pages):
        self.hits = hits
        self.pages = pages

    async def status(self, refresh=False):
        return {'state': 'CONNECTED', 'cloud_available': True}

    async def search(self, query, limit=20):
        return {'source': 'lingxi_cloud', 'total': len(self.hits),
                'results': self.hits[:limit]}

    async def read(self, path, offset=0, max_chars=8000):
        if path not in self.pages:
            raise ConnectorError('PAGE_NOT_FOUND')
        return self.pages[path]


class ProposalValidationTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        paths = [f'wiki/projects/example-old{i}' for i in range(1, 5)]
        pages = {path: page(path, body(supersedes=[], as_of='2026-10-17T12:00:00+08:00'))
                 for path in paths}
        pages['core/核心记忆'] = page('core/核心记忆', '规则')
        session = FakeSession([hit(path) for path in paths], pages)
        content = handoff(supersedes=paths)
        return session, paths, content

    async def test_draft_covering_window_is_accepted(self):
        session, paths, content = self.fixture()
        result = await validate_proposal(session, 'projects', 'Example 交接：新', 'summary',
                                         content, path='wiki/projects/validation-draft')
        self.assertEqual(result['state'], 'ok')
        self.assertEqual(result['selected'], 'wiki/projects/validation-draft')
        self.assertEqual(result['draft_path'], 'wiki/projects/validation-draft')

    async def test_missing_root_is_blocked_before_commit(self):
        session, paths, _ = self.fixture()
        content = handoff(supersedes=paths[:1])
        result = await validate_proposal(session, 'projects', 'Example 交接：新', 'summary',
                                         content, path='wiki/projects/validation-draft')
        self.assertEqual(result['state'], 'blocked')
        self.assertIn(result['reason'], ('RETRIEVAL_CONFLICT', 'RETRIEVAL_AMBIGUOUS',
                                         'RETRIEVAL_PARTIAL', 'DRAFT_NOT_SELECTED'))

    async def test_future_as_of_is_blocked_without_retrieval(self):
        session, paths, _ = self.fixture()
        content = handoff(supersedes=paths, as_of='2099-01-01T00:00:00+08:00')
        result = await validate_proposal(session, 'projects', 'Example 交接：新', 'summary',
                                         content, path='wiki/projects/validation-draft')
        self.assertEqual(result['state'], 'blocked')
        self.assertEqual(result['reason'], 'AS_OF_IN_FUTURE')

    async def test_non_project_is_not_applicable(self):
        session, _, _ = self.fixture()
        result = await validate_proposal(session, 'lessons', '经验', 'summary', '# 经验')
        self.assertEqual(result['state'], 'not_applicable')
