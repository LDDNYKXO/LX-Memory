import tempfile
import unittest
from pathlib import Path
from lingxi_connector.session import Session
from lingxi_connector.cloud import ConnectorError

class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_login_is_unverified_not_logged_out_after_restart(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / 'remember.json').write_text('{"consent":true}')
            s = Session(base)
            self.assertEqual((await s.status())['state'], 'UNKNOWN')
            self.assertFalse((await s.status())['cloud_available'])

    async def test_closed_browser_releases_context_for_remember_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            s = Session(Path(d))
            owned = object()
            s.context = owned
            s.remember = True
            s.state = 'CONNECTED'
            s.context_closed(owned)
            self.assertIsNone(s.context)
            self.assertEqual((await s.status())['state'], 'UNKNOWN')
            # A late close event from an earlier window must not clear its replacement.
            replacement = object()
            s.context = replacement
            s.context_closed(owned)
            self.assertIs(s.context, replacement)

    async def test_empty_session_has_no_cloud_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            s = Session(Path(d))
            self.assertEqual((await s.status())['state'], 'NOT_LOGGED_IN')
            with self.assertRaises(ConnectorError) as e:
                await s.search('测试', 1)
            self.assertEqual(e.exception.code, 'AUTH_REQUIRED')
            self.assertIsNone(s.context)

    async def test_invalid_read_does_not_start_browser(self):
        with tempfile.TemporaryDirectory() as d:
            s = Session(Path(d))
            with self.assertRaises(ConnectorError) as e:
                await s.read('https://evil.test', 0, 8)
            self.assertEqual(e.exception.code, 'INVALID_INPUT')
            self.assertIsNone(s.context)

    async def test_logout_removes_only_owned_profile(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            s = Session(base)
            (base / 'profile').mkdir()
            (base / 'profile' / 'owned.txt').write_text('owned')
            (base / 'unrelated.txt').write_text('keep')
            (base / 'remember.json').write_text('{}')
            await s.logout()
            self.assertFalse((base / 'profile').exists())
            self.assertFalse((base / 'remember.json').exists())
            self.assertEqual((base / 'unrelated.txt').read_text(), 'keep')
            self.assertEqual((await s.status())['state'], 'NOT_LOGGED_IN')
