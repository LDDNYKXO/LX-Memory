import json
import unittest
from lingxi_connector.cloud import (ConnectorError, read_url, search_url,
                                   decode_response, read_result, search_result)

ORIGIN = 'https://lingxi.kdocs.cn'

class CloudTests(unittest.TestCase):
    # Catches allowing arbitrary URLs/path traversal to reach the cloud adapter.
    def test_chinese_path_and_unsafe_inputs(self):
        self.assertEqual(read_url('wiki/concepts/测试'), ORIGIN + '/api/public/v1/agents/default/memory/pages/wiki/concepts/%E6%B5%8B%E8%AF%95')
        for path in ['https://evil.test', 'wiki/../core', 'wiki/a\\b', 'wiki/a?x=1', 'wiki//x', 'wiki/%2e%2e/a', 'core/x#fragment', '/wiki/x']:
            with self.subTest(path=path), self.assertRaises(ConnectorError) as e:
                read_url(path)
            self.assertEqual(e.exception.code, 'INVALID_INPUT')

    def test_query_encoding_and_limit(self):
        self.assertEqual(search_url('a & b', 1), ORIGIN + '/api/public/v1/agents/default/memory/search?query=a+%26+b&max_results=1')
        for q, n in [('', 1), ('x', 0), ('x', 21), ('x', True)]:
            with self.assertRaises(ConnectorError):
                search_url(q, n)

    def test_http_and_html_fail_closed(self):
        for status, url, body, code in [(401, ORIGIN, '{}', 'AUTH_REQUIRED'), (403, ORIGIN, '{}', 'PERMISSION_DENIED'), (429, ORIGIN, '{}', 'RATE_LIMITED'), (500, ORIGIN, '{}', 'NETWORK_ERROR'), (200, 'https://account.wps.cn/login', '<html>', 'AUTH_REQUIRED'), (200, ORIGIN, '<html>', 'UPSTREAM_INCOMPATIBLE'), (200, ORIGIN, '{"result":"error","v7code":7}', 'UPSTREAM_INCOMPATIBLE')]:
            with self.subTest(code=code), self.assertRaises(ConnectorError) as e:
                decode_response(status, url, body)
            self.assertEqual(e.exception.code, code)

    # Catches losing unknown metadata or labelling a partial body as complete.
    def test_read_paging_preserves_metadata(self):
        data = {'path': 'wiki/a', 'front_matter': {'related': ['wiki/b'], 'extra': 3}, 'content': '一二三四五', 'updated': 17}
        out = read_result(data, 1, 2)
        self.assertEqual(out['content'], '二三')
        self.assertEqual(out['front_matter'], data['front_matter'])
        self.assertEqual((out['next_offset'], out['truncated'], out['content_scope']), (3, True, 'partial'))
        self.assertEqual(read_result(data)['content_scope'], 'full')
        with self.assertRaises(ConnectorError):
            read_result(data, -1)

    def test_actual_search_fixture(self):
        from pathlib import Path
        fixture = Path(__file__).resolve().parent / 'fixtures' / 'search.json'
        data = decode_response(200, ORIGIN, fixture.read_text(encoding='utf-8'))
        out = search_result(data)
        self.assertEqual(out['total'], 1)
        self.assertEqual(out['results'][0]['path'], 'wiki/concepts/connector-search-example')
        self.assertNotIn('content', out['results'][0])

if __name__ == '__main__':
    unittest.main()
