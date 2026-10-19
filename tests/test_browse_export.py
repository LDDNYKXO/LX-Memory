"""Browse and export tests: read-only enrichment and local snapshot writing."""
import json
import tempfile
import unittest
from pathlib import Path

from lingxi_connector import collection
from lingxi_connector.cloud import ConnectorError
from lingxi_connector.page_meta import with_meta

HANDOFF = 'wiki/projects/lingxi-connector-abc'
LESSON = 'wiki/lessons/lx-1'


def marker(**fields):
    value = {'schema': 1, 'project': 'LX Memory', 'supersedes': [],
             'as_of': '2026-10-17T00:00:00+08:00', 'record_kind': 'handoff'}
    value.update(fields)
    return '<!-- memory-project-record ' + json.dumps(value, ensure_ascii=False, sort_keys=True) + ' -->'


def page(path, content, title='t', summary='s'):
    return {'path': path, 'front_matter': {'title': title, 'summary': summary, 'sources': [], 'related': []},
            'content': content, 'source': 'lingxi_cloud', 'content_scope': 'full',
            'truncated': False, 'updated': 1, 'retrieved_at': '2026-10-17T00:00:00+00:00'}


class FakeSession:
    def __init__(self):
        self.pages = {
            HANDOFF: page(HANDOFF, marker() + '\n\nhandoff body'),
            LESSON: page(LESSON, with_meta('lesson body', ['教训'], {'领域': '记忆'})),
        }
        self.unreadable = set()
        self.searches = 0

    async def search(self, query, limit=5):
        self.searches += 1
        return {'source': 'lingxi_cloud', 'total': len(self.pages),
                'results': [{'path': p, 'title': 'hit', 'summary': 'hit', 'score': 1.0,
                             'updated': 1} for p in self.pages][:limit]}

    async def read(self, path, offset=0, max_chars=20000):
        if path in self.unreadable:
            raise ConnectorError('UPSTREAM_INCOMPLETE')
        if path not in self.pages:
            raise ConnectorError('PAGE_NOT_FOUND')
        return dict(self.pages[path])


class BrowseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = FakeSession()

    async def test_rows_include_kind_tags_and_as_of(self):
        result = await collection.browse(self.session, '交接')
        self.assertEqual(result['candidates'], 2)
        by_path = {row['path']: row for row in result['rows']}
        self.assertEqual(by_path[HANDOFF]['record_kind'], 'handoff')
        self.assertEqual(by_path[HANDOFF]['as_of'], '2026-10-17T00:00:00+08:00')
        self.assertEqual(by_path[LESSON]['tags'], ['教训'])
        self.assertEqual(by_path[LESSON]['attrs'], {'领域': '记忆'})
        self.assertGreaterEqual(result['reads'], 2)

    async def test_tag_filter_is_exact(self):
        result = await collection.browse(self.session, '记忆', tag='教训')
        self.assertEqual([row['path'] for row in result['rows']], [LESSON])
        result = await collection.browse(self.session, '记忆', tag='教')
        self.assertEqual(result['rows'], [])

    async def test_unreadable_pages_are_skipped(self):
        self.session.unreadable.add(HANDOFF)
        result = await collection.browse(self.session, '交接')
        self.assertEqual([row['path'] for row in result['rows']], [LESSON])

    async def test_invalid_arguments_are_rejected(self):
        for args in (('', 10, ''), ('q', 0, ''), ('q', 21, ''), ('q', 10, 'x' * 33)):
            with self.assertRaises(ConnectorError):
                await collection.browse(self.session, *args)


class ExportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = FakeSession()
        self.root = Path(self.tmp.name) / 'runtime'
        self.root.mkdir()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_export_by_paths_writes_manifest(self):
        result = await collection.export(self.session, paths=[HANDOFF, LESSON], directory=self.root)
        target = Path(result['output'])
        self.assertTrue(target.exists())
        self.assertEqual(result['entries'], 2)
        self.assertEqual(result['skipped'], [])
        data = json.loads(target.read_text(encoding='utf-8'))
        self.assertEqual(data['schema'], 'lingxi-memory/export/v1')
        self.assertEqual([entry['path'] for entry in data['entries']], [HANDOFF, LESSON])
        self.assertEqual(data['entries'][1]['tags'], ['教训'])
        self.assertEqual(len(result['sha256']), 64)

    async def test_export_by_query_uses_search(self):
        result = await collection.export(self.session, query='交接', limit=1, directory=self.root)
        self.assertEqual(result['entries'], 1)
        self.assertEqual(self.session.searches, 1)

    async def test_default_output_lives_under_runtime_exports(self):
        result = await collection.export(self.session, paths=[LESSON], directory=self.root)
        self.assertEqual(Path(result['output']).parent, (self.root / 'exports').resolve())

    async def test_skipped_pages_are_reported(self):
        self.session.unreadable.add(LESSON)
        result = await collection.export(self.session, paths=[HANDOFF, LESSON], directory=self.root)
        self.assertEqual(result['entries'], 1)
        self.assertEqual(result['skipped'], [{'path': LESSON, 'error': 'UPSTREAM_INCOMPLETE'}])

    async def test_relative_or_foreign_output_is_refused(self):
        for output in ('relative/out.json', 'C:/Windows/lx-memory-export-test.json'):
            with self.assertRaises(ConnectorError) as raised:
                await collection.export(self.session, paths=[LESSON], output=output, directory=self.root)
            self.assertEqual(raised.exception.code, 'INVALID_INPUT')

    async def test_requires_query_or_paths(self):
        with self.assertRaises(ConnectorError):
            await collection.export(self.session, directory=self.root)


class BackupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = FakeSession()
        self.root = Path(self.tmp.name) / 'runtime'
        self.root.mkdir()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_backup_writes_snapshot_and_prunes_old_directories(self):
        for _ in range(3):
            result = await collection.backup(self.session, paths=[LESSON],
                                             output_dir=str(self.root / 'backups'), keep=2,
                                             directory=self.root)
        self.assertEqual(result['entries'], 1)
        self.assertEqual(result['keep'], 2)
        dirs = sorted(path for path in (self.root / 'backups').iterdir() if path.is_dir())
        self.assertEqual(len(dirs), 2)
        self.assertTrue((Path(result['snapshot'])).exists())
        self.assertTrue(result['pruned'])

    async def test_default_curated_backup_uses_search_and_skips_missing(self):
        result = await collection.backup(self.session, directory=self.root)
        self.assertEqual(result['entries'], 2)
        self.assertEqual(result['skipped'], [{'path': 'core/核心记忆', 'error': 'PAGE_NOT_FOUND'}])

    async def test_backup_rejects_foreign_output_and_bad_keep(self):
        with self.assertRaises(ConnectorError):
            await collection.backup(self.session, paths=[LESSON], output_dir='C:/Windows/backups',
                                    directory=self.root)
        for keep in (0, 51):
            with self.assertRaises(ConnectorError):
                await collection.backup(self.session, paths=[LESSON], keep=keep, directory=self.root)

    async def test_verify_reports_ok_drift_and_missing(self):
        created = await collection.backup(self.session, paths=[HANDOFF, LESSON], directory=self.root)
        verified = await collection.verify_backup(self.session, created['snapshot'], self.root)
        self.assertEqual((verified['checked'], verified['ok']), (2, 2))
        self.assertEqual(verified['drifted'], [])

        self.session.pages[LESSON]['content'] = with_meta('changed body', ['教训'], {'领域': '记忆'})
        verified = await collection.verify_backup(self.session, created['snapshot'], self.root)
        self.assertEqual([row['path'] for row in verified['drifted']], [LESSON])

        del self.session.pages[HANDOFF]
        verified = await collection.verify_backup(self.session, created['snapshot'], self.root)
        self.assertEqual(verified['missing'], [HANDOFF])

    async def test_verify_rejects_foreign_or_malformed_snapshot(self):
        with self.assertRaises(ConnectorError):
            await collection.verify_backup(self.session, 'C:/Windows/snapshot.json', self.root)
        bad = self.root / 'backups' / 'bad.json'
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text('{not json', encoding='utf-8')
        with self.assertRaises(ConnectorError):
            await collection.verify_backup(self.session, str(bad), self.root)


if __name__ == '__main__':
    unittest.main()
