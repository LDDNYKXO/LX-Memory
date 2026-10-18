"""Tests for tags/attrs embedded as a trailing page metadata block."""
import unittest

from lingxi_connector import page_meta


class PageMetaTests(unittest.TestCase):
    def test_roundtrip_keeps_body_and_metadata(self):
        content = page_meta.with_meta('# 标题\n\n正文', ['偏好', '决策'], {'重要度': 'core'})
        body, tags, attrs = page_meta.split(content)
        self.assertEqual(body, '# 标题\n\n正文')
        self.assertEqual(tags, ['偏好', '决策'])
        self.assertEqual(attrs, {'重要度': 'core'})
        self.assertTrue(content.endswith(' -->'))

    def test_empty_metadata_removes_block(self):
        self.assertEqual(page_meta.with_meta('body', [], {}), 'body')
        content = page_meta.with_meta('body', ['a'], {})
        self.assertEqual(page_meta.with_meta('body', [], {}), page_meta.split(content)[0])

    def test_malformed_or_foreign_block_stays_in_body(self):
        for content in ('body\n\n<!-- lx-memory-meta {bad} -->',
                        'body\n\n<!-- lx-memory-meta {"tags":"x"} -->',
                        'body\n\n<!-- other-meta {"tags":["a"]} -->'):
            body, tags, attrs = page_meta.split(content)
            self.assertEqual(body, content)
            self.assertEqual((tags, attrs), ([], {}))

    def test_invalid_metadata_is_rejected(self):
        for tags, attrs in ((['a'] * 11, {}), (['\n'], {}), ([1], {}),
                            ({}, {'k': 'x' * 201}), ({}, {'k' * 33: 'v'}),
                            ({}, [('k', 'v')])):
            with self.assertRaises(ValueError):
                page_meta.normalize(tags, attrs)

    def test_annotate_adds_tags_and_attrs_only(self):
        page = {'path': 'wiki/lessons/x', 'content': page_meta.with_meta('b', ['教训'], {'领域': '记忆'}),
                'front_matter': {}}
        value = page_meta.annotate(page)
        self.assertEqual(value['tags'], ['教训'])
        self.assertEqual(value['attrs'], {'领域': '记忆'})
        self.assertEqual(value['content'], page['content'])
        self.assertEqual(page.get('tags'), None)

    def test_limit_boundaries_are_accepted(self):
        tags, attrs = page_meta.normalize(['a' * 32] * 10, {'k' * 32: 'v' * 200})
        self.assertEqual(len(tags), 10)
        self.assertEqual(list(attrs), ['k' * 32])


if __name__ == '__main__':
    unittest.main()
