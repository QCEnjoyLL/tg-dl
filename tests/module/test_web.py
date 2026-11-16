"""Regression tests for the Web UI JSON contract, auth and task controls."""

import errno
import os
import tempfile
import unittest
from unittest import mock

import utils
from module import web
from module.download_stat import DownloadState, set_download_state
from utils import atomic_write


SAMPLE_PROGRESS = {
    -100123: {
        1: {
            "down_byte": 50,
            "total_size": 100,
            "file_name": '/tmp/a"b.mp4',
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

SAMPLE_COMPLETED = [
    {
        "chat": -100123,
        "id": 3,
        "file_name": "/tmp/done.mp4",
        "total_size": 4096,
        "completed_time": 1763000000,
        "upload_success": True,
        "upload_state": "uploaded",
    }
]

SAMPLE_CONFIG = """api_id: 12345
api_hash: real-hash-value
bot_token: 123:real-token
media_types:
- video
file_formats:
  audio:
  - all
  document:
  - all
  video:
  - all
chat:
- chat_id: -100123
  last_read_message_id: 7
  download_filter: "id > 10"
web_login_secret: real-secret
proxy:
  scheme: socks5
  hostname: 127.0.0.1
  port: 1080
  password: should-not-leak
"""


class WebApiTestCase(unittest.TestCase):
    def setUp(self):
        flask_app = web.get_flask_app()
        flask_app.config.update(TESTING=True, LOGIN_DISABLED=True)
        self.client = flask_app.test_client()
        set_download_state(DownloadState.Downloading)

    def test_progress_list_is_json(self):
        with mock.patch.object(
            web, "get_download_result", return_value=SAMPLE_PROGRESS
        ):
            response = self.client.get("/get_download_list?already_down=false")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/json")
        rows = response.get_json()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["download_progress"], 50.0)
        self.assertEqual(rows[0]["filename"], 'a"b.mp4')
        self.assertEqual(rows[0]["download_speed"], "10B/s")

    def test_progress_list_zero_size_does_not_divide_by_zero(self):
        # A zero total_size used to raise ZeroDivisionError and return a 500.
        with mock.patch.object(
            web, "get_download_result", return_value=SAMPLE_PROGRESS
        ):
            response = self.client.get("/get_download_list?already_down=false")

        self.assertEqual(response.status_code, 200)
        rows = response.get_json()
        zero_row = next(row for row in rows if row["id"] == 2)
        self.assertEqual(zero_row["download_progress"], 0.0)

    def test_completed_list_is_json(self):
        with mock.patch.object(
            web, "get_completed_downloads", return_value=SAMPLE_COMPLETED
        ):
            response = self.client.get("/get_download_list?already_down=true")

        self.assertEqual(response.status_code, 200)
        rows = response.get_json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["filename"], "done.mp4")
        self.assertTrue(rows[0]["upload_success"])
        self.assertEqual(rows[0]["upload_state"], "uploaded")

    def test_download_list_defaults_to_progress(self):
        # Without an explicit filter the API returns what is in flight.
        with mock.patch.object(
            web, "get_download_result", return_value=SAMPLE_PROGRESS
        ):
            response = self.client.get("/get_download_list")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/json")
        self.assertEqual(len(response.get_json()), 2)

    def test_failed_list_exposes_the_reason(self):
        failed = {
            -100123: {
                132: {
                    "file_name": "sample.mp4",
                    "time": 1763000000,
                    "reason": "only received 0 of 1024 bytes (Timeout: ...)",
                }
            }
        }
        with mock.patch.object(web, "get_failed_downloads", return_value=failed):
            response = self.client.get("/get_failed_list")

        rows = response.get_json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 132)
        # The Web UI shows this column, so the key must be part of the contract.
        self.assertIn("only received 0 of 1024 bytes", rows[0]["reason"])

    def test_download_status_is_json(self):
        with mock.patch.object(
            web, "get_total_download_speed", return_value=1024
        ), mock.patch.object(
            web, "get_completed_downloads", return_value=SAMPLE_COMPLETED
        ):
            response = self.client.get("/get_download_status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/json")
        payload = response.get_json()
        self.assertEqual(payload["download_speed"], "1.0KB/s")
        self.assertEqual(payload["state"], "running")
        # The tab badge reads this without fetching the whole list.
        self.assertEqual(payload["completed"], 1)

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

    def test_pause_task_requires_ids(self):
        response = self.client.post("/pause_task")
        self.assertEqual(response.get_json()["code"], "0")

    def test_pause_and_resume_task(self):
        with mock.patch.object(web, "set_task_paused", return_value=True) as pause:
            response = self.client.post(
                "/pause_task?chat_id=-100123&message_id=7"
            )
        self.assertEqual(response.get_json()["code"], "1")
        pause.assert_called_once_with(-100123, 7, True)

        with mock.patch.object(web, "set_task_paused", return_value=True) as resume:
            response = self.client.post(
                "/resume_task?chat_id=-100123&message_id=7"
            )
        self.assertEqual(response.get_json()["code"], "1")
        resume.assert_called_once_with(-100123, 7, False)

    def test_delete_task(self):
        with mock.patch.object(web, "delete_task") as delete:
            response = self.client.post(
                "/delete_task?chat_id=-100123&message_id=7"
            )
        self.assertEqual(response.get_json()["code"], "1")
        delete.assert_called_once_with(-100123, 7)

    def test_delete_task_cancels_a_downloading_task(self):
        from module import download_stat

        with download_stat._PROGRESS_LOCK:  # pylint: disable = W0212
            download_stat._download_result[-100] = {  # pylint: disable = W0212
                7: {"down_byte": 1}
            }
        try:
            response = self.client.post("/delete_task?chat_id=-100&message_id=7")
            self.assertEqual(response.get_json()["code"], "1")
            self.assertEqual(download_stat.get_download_result(), {})
            self.assertTrue(download_stat.is_task_cancelled(-100, 7))
        finally:
            download_stat.clear_task_cancelled(-100, 7)

    def test_retry_reports_zero_queued_as_failure(self):
        future = mock.Mock()
        future.result.return_value = 0
        with mock.patch.object(web, "_retry_handler", return_value=future):
            response = self.client.post("/retry_failed?chat_id=-100123")

        self.assertEqual(response.get_json()["code"], "0")

    def test_retry_reports_queued_count(self):
        future = mock.Mock()
        future.result.return_value = (2, [])
        with mock.patch.object(web, "_retry_handler", return_value=future):
            response = self.client.post("/retry_failed?chat_id=-100123")

        payload = response.get_json()
        self.assertEqual(payload["code"], "1")
        self.assertEqual(payload["retried"], 2)

    def test_retry_reports_the_reason_when_nothing_queued(self):
        future = mock.Mock()
        future.result.return_value = (0, ["消息 132 已不可访问"])
        with mock.patch.object(web, "_retry_handler", return_value=future):
            response = self.client.post(
                "/retry_failed?chat_id=-100123&message_id=132"
            )

        payload = response.get_json()
        self.assertEqual(payload["code"], "0")
        self.assertEqual(payload["msg"], "消息 132 已不可访问")

    def test_retry_with_an_invalid_message_id_does_not_retry_the_chat(self):
        """A typo used to fall back to "retry every failed message"."""
        handler = mock.Mock()
        with mock.patch.object(web, "_retry_handler", handler):
            response = self.client.post(
                "/retry_failed?chat_id=-100123&message_id=abc"
            )

        payload = response.get_json()
        self.assertEqual(payload["code"], "0")
        self.assertIn("message_id", payload["msg"])
        handler.assert_not_called()

    def test_retry_timeout_is_reported_as_submitted(self):
        """The coroutine keeps running, so it must not answer "failed"."""
        future = mock.Mock()
        future.result.side_effect = web.futures.TimeoutError()
        with mock.patch.object(web, "_retry_handler", return_value=future):
            response = self.client.post("/retry_failed?chat_id=-100123")

        payload = response.get_json()
        self.assertEqual(payload["code"], "1")
        self.assertTrue(payload["pending"])

    def test_delete_task_accepts_a_text_chat_id(self):
        # The table renders the chat id as text; the handler must convert it back
        # to int or the delete silently misses the in-memory task dict.
        with mock.patch.object(web, "delete_task") as delete:
            self.client.post("/delete_task?chat_id=-100123&message_id=7")
        delete.assert_called_once_with(-100123, 7)

    def test_retry_accepts_a_username_chat(self):
        future = mock.Mock()
        future.result.return_value = (1, [])
        with mock.patch.object(web, "_retry_handler") as handler:
            handler.return_value = future
            response = self.client.post("/retry_failed?chat_id=%40mychannel")

        self.assertEqual(response.get_json()["code"], "1")
        self.assertEqual(handler.call_args[0][0], "@mychannel")


class _ConfigFixture(unittest.TestCase):
    def setUp(self):
        flask_app = web.get_flask_app()
        flask_app.config.update(TESTING=True, LOGIN_DISABLED=True)
        self.client = flask_app.test_client()
        self.tempdir = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.tempdir.name, "config.yaml")
        with open(self.config_path, "w", encoding="utf-8") as config_file:
            config_file.write(SAMPLE_CONFIG)

        self.app_patch = mock.patch.object(web, "_application")
        app = self.app_patch.start()
        app.config_file = "config.yaml"
        app.reload_hot_config.return_value = (["media_types"], ["api_id"])
        app.web_login_secret = ""
        self.app = app
        # Point the editor at the temp file regardless of the current drive.
        self.path_patch = mock.patch.object(
            web, "_config_path", return_value=self.config_path
        )
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.app_patch.stop()
        self.tempdir.cleanup()

    def saved_config(self) -> dict:
        from ruamel import yaml

        with open(self.config_path, encoding="utf-8") as config_file:
            return yaml.YAML().load(config_file.read())


class WebConfigTestCase(_ConfigFixture):

    def test_get_config_masks_secrets(self):
        response = self.client.get("/get_config")
        payload = response.get_json()

        self.assertEqual(payload["code"], "1")
        self.assertIn("api_hash: ********", payload["text"])
        self.assertIn("bot_token: ********", payload["text"])
        self.assertIn("password: ********", payload["text"])
        self.assertNotIn("real-hash-value", payload["text"])
        self.assertNotIn("real-token", payload["text"])
        self.assertNotIn("should-not-leak", payload["text"])

    def test_set_config_restores_masked_secrets(self):
        masked = web._mask_config_text(SAMPLE_CONFIG)
        response = self.client.post("/set_config", data={"text": masked})

        self.assertEqual(response.get_json()["code"], "1")
        with open(self.config_path, encoding="utf-8") as config_file:
            saved = config_file.read()
        self.assertIn("real-hash-value", saved)
        self.assertIn("real-token", saved)

    def test_set_config_rejects_invalid_yaml(self):
        response = self.client.post("/set_config", data={"text": "api_id: [unclosed"})
        payload = response.get_json()

        self.assertEqual(payload["code"], "0")
        self.assertIn("YAML", payload["msg"])

    def test_set_config_rejects_missing_required_keys(self):
        response = self.client.post("/set_config", data={"text": "api_id: 1"})
        payload = response.get_json()

        self.assertEqual(payload["code"], "0")
        self.assertIn("api_hash", payload["msg"])


class ConfigFormTestCase(_ConfigFixture):
    def base_form(self, **overrides):
        data = {
            "api_id": "12345",
            "api_hash": web.CONFIG_MASK,
            "media_types": ["video"],
            "chat": "-100123",
        }
        # The browser always renders the presence marker for switches and
        # checkbox groups; without it the server keeps the stored value.
        for field in web.config_form.FIELDS:
            if field["type"] in ("bool", "multiselect"):
                data[field["name"] + "__present"] = "1"
        data.update(overrides)
        return data

    def test_form_payload_has_schema_values_and_masked_secrets(self):
        response = self.client.get("/get_config_form")
        payload = response.get_json()

        self.assertEqual(payload["code"], "1")
        self.assertIn("基础", payload["groups"])
        names = [field["name"] for field in payload["fields"]]
        self.assertIn("media_types", names)
        self.assertIn("upload_drive.enable_upload_file", names)

        values = payload["values"]
        self.assertEqual(values["api_id"], "12345")
        self.assertEqual(values["api_hash"], web.CONFIG_MASK)
        self.assertEqual(values["bot_token"], web.CONFIG_MASK)
        self.assertEqual(values["chat"], "-100123")
        self.assertEqual(values["media_types"], ["video"])
        self.assertNotIn("real-hash-value", response.get_data(as_text=True))

    def test_form_save_applies_values_and_keeps_masked_secrets(self):
        response = self.client.post(
            "/set_config_form",
            data=self.base_form(
                media_types=["video", "document"],
                max_download_task="3",
                hide_file_name="1",
                chat="-100123\n@new_channel",
            ),
        )
        payload = response.get_json()
        self.assertEqual(payload["code"], "1", payload)

        saved = self.saved_config()
        self.assertEqual(saved["api_hash"], "real-hash-value")
        self.assertEqual(saved["bot_token"], "123:real-token")
        self.assertEqual(saved["max_download_task"], 3)
        self.assertTrue(saved["hide_file_name"])
        self.assertEqual(saved["media_types"], ["video", "document"])

        chats = saved["chat"]
        self.assertEqual(len(chats), 2)
        # Per-chat settings survive an edit of the chat list.
        self.assertEqual(chats[0]["chat_id"], -100123)
        self.assertEqual(chats[0]["download_filter"], "id > 10")
        self.assertEqual(chats[0]["last_read_message_id"], 7)
        self.assertEqual(chats[1]["chat_id"], "@new_channel")

    def test_form_save_clears_blank_keys(self):
        response = self.client.post(
            "/set_config_form",
            data=self.base_form(web_port="", save_path=""),
        )
        self.assertEqual(response.get_json()["code"], "1")

        saved = self.saved_config()
        self.assertNotIn("save_path", saved)

    def test_form_rejects_empty_required_field(self):
        response = self.client.post("/set_config_form", data=self.base_form(api_id=""))
        payload = response.get_json()

        self.assertEqual(payload["code"], "0")
        self.assertIn("api_id", payload["errors"])

    def test_form_rejects_bad_number_and_empty_media_types(self):
        response = self.client.post(
            "/set_config_form",
            data=self.base_form(max_download_task="abc", media_types=[]),
        )
        errors = response.get_json()["errors"]

        self.assertIn("max_download_task", errors)
        self.assertIn("media_types", errors)

    def test_form_rejects_out_of_range_number(self):
        response = self.client.post(
            "/set_config_form", data=self.base_form(web_port="70000")
        )
        errors = response.get_json()["errors"]

        self.assertIn("web_port", errors)

    def test_form_shows_file_formats_joined(self):
        payload = self.client.get("/get_config_form").get_json()

        self.assertEqual(payload["values"]["file_formats.audio"], "all")
        self.assertEqual(payload["values"]["file_formats.video"], "all")

    def test_form_saves_file_formats_as_lists(self):
        data = self.base_form()
        data.update(
            {
                "file_formats.audio": "mp3, flac",
                # Users paste YAML list syntax; it must still become a list.
                "file_formats.video": "['mkv','mp4']",
            }
        )
        response = self.client.post("/set_config_form", data=data)
        self.assertEqual(response.get_json()["code"], "1")

        saved = self.saved_config()
        self.assertEqual(list(saved["file_formats"]["audio"]), ["mp3", "flac"])
        self.assertEqual(list(saved["file_formats"]["video"]), ["mkv", "mp4"])

    def test_form_reads_legacy_chat_id(self):
        legacy = (
            "api_id: 12345\n"
            "api_hash: real-hash-value\n"
            "media_types:\n- video\n"
            "chat_id: -100999\n"
            "last_read_message_id: 3\n"
        )
        with open(self.config_path, "w", encoding="utf-8") as config_file:
            config_file.write(legacy)

        payload = self.client.get("/get_config_form").get_json()
        self.assertEqual(payload["values"]["chat"], "-100999")

    def test_form_ignores_empty_chat_entries(self):
        broken = (
            "api_id: 12345\n"
            "api_hash: real-hash-value\n"
            "media_types:\n- video\n"
            "chat:\n"
            "- chat_id:\n"
            "  last_read_message_id:\n"
        )
        with open(self.config_path, "w", encoding="utf-8") as config_file:
            config_file.write(broken)

        payload = self.client.get("/get_config_form").get_json()
        # A null chat_id must not be rendered as the literal string "None".
        self.assertEqual(payload["values"]["chat"], "")

        response = self.client.post(
            "/set_config_form", data=self.base_form(chat="-100123")
        )
        self.assertEqual(response.get_json()["code"], "1")
        saved = self.saved_config()
        self.assertEqual(len(saved["chat"]), 1)
        self.assertEqual(saved["chat"][0]["chat_id"], -100123)

    def test_form_updates_proxy_block(self):
        data = self.base_form()
        data.update(
            {
                "proxy.scheme": "http",
                "proxy.hostname": "10.0.0.1",
                "proxy.port": "8080",
                "proxy.username": "user",
                "proxy.password": web.CONFIG_MASK,
            }
        )
        response = self.client.post("/set_config_form", data=data)
        self.assertEqual(response.get_json()["code"], "1")

        saved = self.saved_config()
        self.assertEqual(saved["proxy"]["scheme"], "http")
        self.assertEqual(saved["proxy"]["hostname"], "10.0.0.1")
        self.assertEqual(saved["proxy"]["port"], 8080)
        self.assertEqual(saved["proxy"]["password"], "should-not-leak")


class WebAssetTestCase(unittest.TestCase):
    """The UI must never run a cached script against newer markup."""

    def setUp(self):
        flask_app = web.get_flask_app()
        flask_app.config.update(TESTING=True, LOGIN_DISABLED=True)
        self.client = flask_app.test_client()

    def test_index_uses_versioned_assets_and_no_store(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('data-version="%s"' % utils.__version__, html)
        self.assertIn("app.css?v=", html)
        self.assertIn("app.js?v=", html)
        self.assertIn("TeleVault", html)
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))

    def test_static_assets_are_long_lived(self):
        response = self.client.get("/static/js/app.js?v=1")

        self.assertEqual(response.status_code, 200)
        self.assertIn("immutable", response.headers.get("Cache-Control", ""))

    def test_login_page_is_versioned_too(self):
        response = self.client.get("/login")

        html = response.get_data(as_text=True)
        self.assertIn("data-version=", html)
        self.assertIn("login.css?v=", html)

    def test_app_version_endpoint(self):
        response = self.client.get("/get_app_version")

        self.assertEqual(response.get_data(as_text=True).strip(), utils.__version__)


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

    def test_repeated_failures_lock_the_client_out(self):
        try:
            web._login_failures.clear()  # pylint: disable = W0212
            with mock.patch.dict(
                web.web_login_users, {"root": "s3cret"}, clear=True
            ):
                for _ in range(web._LOGIN_MAX_FAILURES):  # pylint: disable = W0212
                    self.client.post("/login", data={"password": "wrong"})
                # Even the right password must not be accepted while locked out.
                response = self.client.post("/login", data={"password": "s3cret"})

            payload = response.get_json()
            self.assertEqual(payload["code"], "0")
            self.assertIn("尝试次数过多", payload["msg"])
        finally:
            web._login_failures.clear()  # pylint: disable = W0212

    def test_successful_login_clears_the_failure_counter(self):
        try:
            web._login_failures.clear()  # pylint: disable = W0212
            with mock.patch.dict(
                web.web_login_users, {"root": "s3cret"}, clear=True
            ):
                self.client.post("/login", data={"password": "wrong"})
                response = self.client.post("/login", data={"password": "s3cret"})

            self.assertEqual(response.get_json()["code"], "1")
            self.assertEqual(web._login_failures, {})  # pylint: disable = W0212
        finally:
            web._login_failures.clear()  # pylint: disable = W0212
            self.client.post("/logout")

    def test_stale_failure_entries_are_pruned(self):
        """An address that guessed once and left must not be remembered forever.

        Public scanners hit /login from a new address every few seconds; the
        dict has to stay bounded, or the process leaks memory over weeks.
        """
        try:
            web._login_failures.clear()  # pylint: disable = W0212
            web._login_last_prune = float("-inf")  # pylint: disable = W0212
            # A scanner that tried once long ago and never came back.
            web._login_failures["203.0.113.7"] = (  # pylint: disable = W0212
                1,
                -web._LOGIN_FAILURE_WINDOW_SECONDS * 2,  # pylint: disable = W0212
                0.0,
            )
            with mock.patch.dict(
                web.web_login_users, {"root": "s3cret"}, clear=True
            ):
                self.client.post("/login", data={"password": "wrong"})

            self.assertNotIn(
                "203.0.113.7", web._login_failures  # pylint: disable = W0212
            )
            # The current client is still counted, so pruning is not a bypass.
            self.assertEqual(len(web._login_failures), 1)  # pylint: disable = W0212
        finally:
            web._login_failures.clear()  # pylint: disable = W0212

    def test_lockout_allows_retry_after_it_expires(self):
        """The lockout must end: one more failure must not re-lock immediately."""
        try:
            web._login_failures.clear()  # pylint: disable = W0212
            web._login_last_prune = float("-inf")  # pylint: disable = W0212
            with mock.patch.dict(
                web.web_login_users, {"root": "s3cret"}, clear=True
            ):
                for _ in range(web._LOGIN_MAX_FAILURES):  # pylint: disable = W0212
                    self.client.post("/login", data={"password": "wrong"})

                # Pretend the lockout is over.
                for client_ip in list(web._login_failures):  # pylint: disable = W0212
                    failures, last_attempt, _ = web._login_failures[  # pylint: disable = W0212
                        client_ip
                    ]
                    web._login_failures[client_ip] = (  # pylint: disable = W0212
                        failures,
                        last_attempt,
                        -1.0,
                    )

                response = self.client.post("/login", data={"password": "s3cret"})

            self.assertEqual(response.get_json()["code"], "1")
        finally:
            web._login_failures.clear()  # pylint: disable = W0212
            self.client.post("/logout")

    def test_secret_key_is_not_the_public_upstream_value(self):
        self.assertNotEqual(web.get_flask_app().secret_key, "tdl")

    def test_empty_password_forces_loopback(self):
        app = mock.Mock()
        app.web_host = "0.0.0.0"
        app.web_login_secret = ""
        self.assertEqual(web._effective_web_host(app), "127.0.0.1")

        app.web_login_secret = "configured"
        self.assertEqual(web._effective_web_host(app), "0.0.0.0")

    def test_logout_clears_session(self):
        with mock.patch.dict(web.web_login_users, {"root": "s3cret"}, clear=True):
            self.client.post("/login", data={"password": "s3cret"})
            response = self.client.post("/logout")

        self.assertEqual(response.get_json(), {"code": "1"})
        # After logout the index redirects to the login page.
        index_response = self.client.get("/")
        self.assertEqual(index_response.status_code, 302)
        self.assertIn("/login", index_response.headers["Location"])


class WriteConfigAtomicTestCase(unittest.TestCase):
    """Saving config.yaml must never truncate the existing file."""

    def _write_old(self, tmp_dir):
        path = os.path.join(tmp_dir, "config.yaml")
        with open(path, "w", encoding="utf-8") as config_file:
            config_file.write("old: value\n")
        return path

    def _read(self, path):
        with open(path, encoding="utf-8") as config_file:
            return config_file.read()

    def test_bind_mount_fallback_copies_complete_text(self):
        """os.replace on a bind-mounted file fails with EBUSY; copy instead."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = self._write_old(tmp_dir)
            with mock.patch.object(
                atomic_write.os, "replace", side_effect=OSError(errno.EBUSY, "busy")
            ):
                atomic_write.write_text_atomic(path, "new: value\n")

            self.assertEqual(self._read(path), "new: value\n")

    def test_failed_copy_raises_and_cleans_the_temp_file(self):
        """A failing fallback write surfaces the error instead of hiding it."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = self._write_old(tmp_dir)
            with mock.patch.object(
                atomic_write.os, "replace", side_effect=OSError(errno.EBUSY, "busy")
            ), mock.patch.object(
                atomic_write.shutil, "copyfile", side_effect=OSError(errno.ENOSPC, "full")
            ):
                with self.assertRaises(OSError):
                    atomic_write.write_text_atomic(path, "new: value\n")

            self.assertEqual(self._read(path), "old: value\n")
            self.assertEqual(
                [name for name in os.listdir(tmp_dir) if name != "config.yaml"], []
            )

    def test_disk_full_propagates_without_touching_the_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = self._write_old(tmp_dir)
            with mock.patch.object(
                atomic_write.os, "replace", side_effect=OSError(errno.ENOSPC, "full")
            ):
                with self.assertRaises(OSError):
                    atomic_write.write_text_atomic(path, "new: value\n")

            self.assertEqual(self._read(path), "old: value\n")
