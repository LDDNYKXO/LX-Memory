from pathlib import Path
import io
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
    def test_agent_facing_output_is_forced_to_utf8(self):
        # A pipe keeps the machine ANSI code page; the live CLI proved that as cp936 bytes, which a
        # UTF-8 reader cannot decode. Both stdout and stderr carry JSON for the calling agent.
        ns=runpy.run_path(str(ROOT/'run.py'),run_name='bootstrap_test')
        class Stream:
            def __init__(self):self.reconfigured=None
            def reconfigure(self,**kwargs):self.reconfigured=kwargs
        out,err=Stream(),Stream()
        with patch.object(sys,'stdout',out),patch.object(sys,'stderr',err):
            ns['use_utf8_streams']()
        self.assertEqual(out.reconfigured,{'encoding':'utf-8'})
        self.assertEqual(err.reconfigured,{'encoding':'utf-8'})
    def test_utf8_switch_tolerates_streams_that_cannot_be_reconfigured(self):
        ns=runpy.run_path(str(ROOT/'run.py'),run_name='bootstrap_test')
        with patch.object(sys,'stdout',io.StringIO()),patch.object(sys,'stderr',io.StringIO()):
            ns['use_utf8_streams']()

if __name__=='__main__':
    unittest.main()
