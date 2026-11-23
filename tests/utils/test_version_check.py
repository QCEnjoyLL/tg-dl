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
        version_check._last_error = None
        version_check._proxy = None

    def tearDown(self):
        self.setUp()

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
        # Offline, DNS-blocked or rate limited: the app must keep working, but
        # the reason is exposed so the UI can explain a failed manual check.
        with mock.patch.object(
            version_check, "_fetch", side_effect=OSError("offline")
        ):
            version_check.refresh(force=True)

        self.assertIsNone(version_check._latest)
        self.assertIn("offline", version_check.update_info()["error"])

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
        self.assertEqual(info["error"], "")

    def test_http_proxy_uses_proxy_handler(self):
        payload = b'{"tag_name": "v99.2.0", "html_url": "https://example.test/r"}'
        response = mock.MagicMock()
        response.read.return_value = payload
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)
        opener = mock.MagicMock()
        opener.open.return_value = response

        version_check.set_proxy(
            {
                "scheme": "http",
                "hostname": "127.0.0.1",
                "port": 8080,
                "username": "u:name",
                "password": "p@ss",
            }
        )
        with (
            mock.patch.object(
                version_check.urllib.request, "ProxyHandler", wraps=version_check.urllib.request.ProxyHandler
            ) as handler_mock,
            mock.patch.object(
                version_check.urllib.request, "build_opener", return_value=opener
            ),
        ):
            version_check.refresh(force=True)

        proxies = handler_mock.call_args.args[0]
        self.assertEqual(
            proxies["https"], "http://u%3Aname:p%40ss@127.0.0.1:8080"
        )
        self.assertEqual(version_check.update_info()["latest"], "99.2.0")

    def test_socks5_proxy_uses_socks_connection_with_rdns(self):
        version_check.set_proxy(
            {
                "scheme": "socks5",
                "hostname": "10.0.0.2",
                "port": 1080,
                "username": "alice",
                "password": "secret",
            }
        )
        raw_sock = mock.MagicMock()

        def fake_do_open(conn_cls, req, context=None):
            conn = conn_cls("api.github.com", context=context)
            with mock.patch.object(
                conn._context, "wrap_socket", return_value=raw_sock
            ):
                conn.connect()
            resp = mock.MagicMock()
            resp.code = 200
            resp.status = 200
            resp.msg = "OK"
            resp.read.return_value = (
                b'{"tag_name": "v99.3.0", "html_url": "https://example.test/s"}'
            )
            resp.__enter__ = mock.Mock(return_value=resp)
            resp.__exit__ = mock.Mock(return_value=False)
            return resp

        with (
            mock.patch("socks.create_connection", return_value=raw_sock) as socks_mock,
            mock.patch.object(
                version_check.urllib.request.HTTPSHandler,
                "do_open",
                side_effect=fake_do_open,
            ),
        ):
            version_check.refresh(force=True)

        self.assertEqual(socks_mock.call_count, 1)
        kw = socks_mock.call_args.kwargs
        self.assertEqual(kw["proxy_addr"], "10.0.0.2")
        self.assertEqual(kw["proxy_port"], 1080)
        self.assertTrue(kw["proxy_rdns"])
        self.assertEqual(kw["proxy_username"], "alice")
        self.assertEqual(kw["proxy_password"], "secret")
        self.assertEqual(version_check.update_info()["latest"], "99.3.0")

    def test_proxy_credentials_are_redacted_in_error_info(self):
        with mock.patch.object(
            version_check,
            "_fetch",
            side_effect=OSError("proxy failed http://alice:topsecret@127.0.0.1:8080"),
        ):
            version_check.refresh(force=True)

        err = version_check.update_info()["error"]
        self.assertNotIn("alice:topsecret", err)
        self.assertIn("http://***@127.0.0.1:8080", err)


if __name__ == "__main__":
    unittest.main()
