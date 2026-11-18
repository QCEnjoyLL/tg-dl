"""Tests for the cached GitHub release check (bot card + Web UI hint)."""

import os
import time
import unittest
from unittest import mock

from utils import version_check


class VersionParsingTestCase(unittest.TestCase):
    def test_parse_version(self):
        self.assertEqual(version_check.parse_version("v2.13.2"), (2, 13, 2))
        self.assertEqual(version_check.parse_version("2.13.2"), (2, 13, 2))
        self.assertEqual(version_check.parse_version(2.13), (2, 13))
        self.assertEqual(version_check.parse_version(""), (0,))

    def test_is_newer(self):
        self.assertTrue(version_check.is_newer("2.14.0", "2.13.2"))
        self.assertTrue(version_check.is_newer("v2.13.3", "2.13.2"))
        self.assertFalse(version_check.is_newer("2.13.2", "2.13.2"))
        # "2.13" and "2.13.0" are the same release, not an update.
        self.assertFalse(version_check.is_newer("2.13", "2.13.0"))
        self.assertFalse(version_check.is_newer(None, "2.13.2"))
        self.assertFalse(version_check.is_newer("", "2.13.2"))


class RefreshTestCase(unittest.TestCase):
    def setUp(self):
        version_check._latest = None
        version_check._checked_at = 0.0
        version_check._url = version_check.RELEASES_URL

    def tearDown(self):
        self.setUp()

    def test_update_info_is_empty_before_the_first_check(self):
        info = version_check.update_info()

        self.assertFalse(info["newer"])
        self.assertEqual(info["latest"], "")
        self.assertEqual(info["current"], version_check.utils.__version__)
        self.assertEqual(info["checked_at"], 0)

    def test_update_info_reports_a_newer_release(self):
        version_check._latest = "99.0.0"
        version_check._checked_at = 123456

        info = version_check.update_info()

        self.assertTrue(info["newer"])
        self.assertEqual(info["latest"], "99.0.0")
        self.assertEqual(info["checked_at"], 123456)

    def test_refresh_respects_the_ttl(self):
        version_check._checked_at = time.time()

        with mock.patch.object(version_check, "_fetch") as fetch:
            version_check.refresh()
        fetch.assert_not_called()

        with mock.patch.object(version_check, "_fetch") as fetch:
            version_check.refresh(force=True)
        fetch.assert_called_once_with()

    def test_refresh_swallows_network_errors(self):
        # Offline, DNS-blocked or rate limited: the app must keep working.
        with mock.patch.object(
            version_check, "_fetch", side_effect=OSError("offline")
        ):
            version_check.refresh(force=True)

        self.assertIsNone(version_check._latest)

    def test_environment_kill_switch(self):
        with mock.patch.dict(os.environ, {"TDL_VERSION_CHECK": "0"}):
            self.assertFalse(version_check.version_check_enabled())
            with mock.patch.object(version_check, "_fetch") as fetch:
                version_check.refresh(force=True)

        fetch.assert_not_called()

    def test_fetch_stores_the_release(self):
        payload = (
            b'{"tag_name": "v99.1.0", '
            b'"html_url": "https://example.test/releases/tag/v99.1.0"}'
        )
        response = mock.MagicMock()
        response.read.return_value = payload
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)

        with mock.patch.object(
            version_check.urllib.request, "urlopen", return_value=response
        ):
            version_check.refresh(force=True)

        info = version_check.update_info()
        self.assertEqual(info["latest"], "99.1.0")
        self.assertTrue(info["newer"])
        self.assertEqual(info["url"], "https://example.test/releases/tag/v99.1.0")
        self.assertGreater(info["checked_at"], 0)


if __name__ == "__main__":
    unittest.main()
