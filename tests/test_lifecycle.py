import json
import unittest
from pathlib import Path
from lingxi_connector.lifecycle import handle_event


class LifecycleTests(unittest.TestCase):
    def test_short_resume_prompt_retrieves_once_and_is_data(self):
        calls = []
        def fetch(root, operation, args):
            calls.append((operation, args))
            return {'state': 'partial', 'goal': '', 'unknowns': ['missing project']}
        result = handle_event(Path('.'), {'prompt': '继续', 'session_id': 's1', 'turn_id': 't1', 'cwd': 'D:/CodeX'}, fetch)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 'context')
        self.assertEqual(calls[0][1]['request_id'], 's1:t1')
        text = result['hookSpecificOutput']['additionalContext']
        self.assertIn('partial', text)
        self.assertIn('背景数据', text)

    def test_unrelated_request_never_contacts_memory(self):
        def fail(*args):
            self.fail('independent translation should not retrieve memory')
        self.assertEqual(handle_event(Path('.'), {'prompt': '把 hello 翻译成中文'}, fail), {})

    def test_transport_failure_explicitly_degrades(self):
        from lingxi_connector.cloud import ConnectorError
        def fail(*args):
            raise ConnectorError('NETWORK_ERROR')
        result = handle_event(Path('.'), {'prompt': '继续这个项目'}, fail)
        self.assertIn('service_unavailable', result['hookSpecificOutput']['additionalContext'])
