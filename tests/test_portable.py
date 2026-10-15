from pathlib import Path
import runpy
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
class PortableTests(unittest.TestCase):
    def test_bundled_python_uses_matching_dependencies(self):
        original=sys.path[:]
        try:
            with patch.object(sys,'executable',str(ROOT/'vendor/python/python.exe')):
                ns=runpy.run_path(str(ROOT/'run.py'),run_name='bootstrap_test')
            self.assertEqual(ns['dependency_root'],ROOT/'vendor/deps')
        finally:sys.path[:]=original
    def test_legacy_python_keeps_original_dependencies(self):
        original=sys.path[:]
        try:
            with patch.object(sys,'executable',str(ROOT/'external-python/python.exe')):
                ns=runpy.run_path(str(ROOT/'run.py'),run_name='bootstrap_test')
            self.assertEqual(ns['dependency_root'],ROOT/'runtime/deps')
        finally:sys.path[:]=original
