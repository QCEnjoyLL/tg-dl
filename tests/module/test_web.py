"""Regression tests for the Web UI JSON contract and login handling."""

import unittest
from unittest import mock

from module import web
from module.download_stat import DownloadState, set_download_state


SAMPLE_DOWNLOADS = {
    -100123: {
        1: {
            "down_byte": 50,
            "total_size": 100,
            "file_name": "/tmp/a\"b.mp4",
            "start_time": 1,
            "end_time": 2,
            "download_speed": 10,
            "each_second_total_download": 0,
            "task_id": 1,
        },
        2: {
            "down_byte": 0,
            "total_size": 0,
            "file_name": "/tmp/unknown.bin",
            "start_time": 1,
            "end_time": 2,
            "download_speed": 0,
            "each_second_total_download": 0,
            "task_id": 1,
        },
    }
}


class WebApiTestCase(unittest.TestCase):
    def setUp(self):
        flask_app = web.get_flask_app()
        flask_app.config.update(TESTING=True, LOGIN_DISABLED=True)
        self.client = flask_app.test_client()
        set_download_state(DownloadState.Downloading)

    def test_download_list_is_json(self):
        with mock.patch.object(
            web, "get_download_result", return_value=SAMPLE_DOWNLOADS
        ):
            response = self.client.get("/get_download_list?already_down=false")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/json")
        rows = response.get_json()
        # The zero-size row is treated as already finished (0 == 0) and is
        # filtered out by already_down=false.
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["download_progress"], 50.0)
        self.assertEqual(rows[0]["filename"], 'a"b.mp4')
        self.assertEqual(rows[0]["download_speed"], "10B/s")

    def test_download_list_zero_size_does_not_divide_by_zero(self):
        # A zero total_size used to raise ZeroDivisionError and return a 500.
        with mock.patch.object(
            web, "get_download_result", return_value=SAMPLE_DOWNLOADS
        ):
            response = self.client.get("/get_download_list?already_down=true")

        self.assertEqual(response.status_code, 200)
        rows = response.get_json()
        self.assertTrue(any(row["id"] == 2 for row in rows))
        self.assertEqual(
            next(row for row in rows if row["id"] == 2)["download_progress"], 0.0
        )

    def test_download_list_without_filter_returns_empty_array(self):
        response = self.client.get("/get_download_list")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/json")
        self.assertEqual(response.get_json(), [])

    def test_download_status_is_json(self):
        with mock.patch.object(web, "get_total_download_speed", return_value=1024):
            response = self.client.get("/get_download_status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/json")
        self.assertEqual(response.get_json()["download_speed"], "1.0KB/s")

    def test_set_download_state_rejects_unknown_values(self):
        response = self.client.post(
            "/set_download_state?state=<script>alert(1)</script>"
        )
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(b"<script>", response.data)

    def test_set_download_state_returns_plain_text(self):
        response = self.client.post("/set_download_state?state=pause")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), "continue")
        self.assertEqual(web.get_download_state(), DownloadState.StopDownload)


class WebLoginTestCase(unittest.TestCase):
    def setUp(self):
        flask_app = web.get_flask_app()
        flask_app.config.update(TESTING=True, LOGIN_DISABLED=False)
        self.client = flask_app.test_client()

    def test_plaintext_password_is_compared(self):
        # Client-side AES was removed with the hardcoded key in the page; the
        # backend must accept the plain form value over the TLS boundary.
        with mock.patch.dict(web.web_login_users, {"root": "s3cret"}, clear=True):
            response = self.client.post("/login", data={"password": "s3cret"})

        self.assertEqual(response.get_json(), {"code": "1"})

    def test_wrong_password_is_rejected(self):
        with mock.patch.dict(web.web_login_users, {"root": "s3cret"}, clear=True):
            response = self.client.post("/login", data={"password": "nope"})

        self.assertEqual(response.get_json(), {"code": "0"})

    def test_malformed_password_does_not_500(self):
        with mock.patch.dict(web.web_login_users, {"root": "s3cret"}, clear=True):
            response = self.client.post("/login", data={"password": "not-a-cipher"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"code": "0"})

    def test_secret_key_is_not_the_public_upstream_value(self):
        self.assertNotEqual(web.get_flask_app().secret_key, "tdl")

    def test_empty_password_forces_loopback(self):
        app = mock.Mock()
        app.web_host = "0.0.0.0"
        app.web_login_secret = ""
        self.assertEqual(web._effective_web_host(app), "127.0.0.1")

        app.web_login_secret = "configured"
        self.assertEqual(web._effective_web_host(app), "0.0.0.0")
