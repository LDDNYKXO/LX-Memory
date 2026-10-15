import unittest
from lingxi_connector.wizard import selection
from lingxi_connector.setup import InstallerError

class WizardTests(unittest.TestCase):
    def test_simple_selection_and_empty_default(self):
        self.assertEqual(selection('1,3', ['Codex']),['Codex','WorkBuddy'])
        self.assertEqual(selection('', ['DSH']),['DSH'])
    def test_invalid_selection_is_rejected(self):
        for text in ('0','1,1','5','oops'):
            with self.assertRaises(InstallerError):selection(text,['Codex'])
