"""test app"""

import os
import sys
import tempfile
import unittest

from ruamel import yaml

import module.app
from module.app import Application, ChatDownloadConfig, DownloadStatus, TaskNode

sys.path.append("..")  # Adds higher directory to python modules path.


class AppTestCase(unittest.TestCase):
    def test_downloading_status_is_not_counted(self):
        node = TaskNode(123)
        node.stat(DownloadStatus.Downloading)
        self.assertEqual(node.total_download_task, 0)
        self.assertEqual(node.failed_download_task, 0)

        node.stat(DownloadStatus.SuccessDownload)
        self.assertEqual(node.total_download_task, 1)
        self.assertEqual(node.success_download_task, 1)

    @classmethod
    def tearDownClass(cls):
        config_test = os.path.join(os.path.abspath("."), "config_test.yaml")
        data_test = os.path.join(os.path.abspath("."), "data_test.yaml")
        if os.path.exists(config_test):
            os.remove(config_test)
        if os.path.exists(data_test):
            os.remove(data_test)

    def test_app(self):
        app = Application("", "")
        self.assertEqual(app.save_path, os.path.join(os.path.abspath("."), "downloads"))
        self.assertEqual(app.proxy, {})
        self.assertEqual(app.restart_program, False)

        app.chat_download_config[123] = ChatDownloadConfig()
        app.chat_download_config[123].last_read_message_id = 13
        app.chat_download_config[123].node.download_status[
            6
        ] = DownloadStatus.Downloading
        app.chat_download_config[123].ids_to_retry.append(7)
        # download success
        app.chat_download_config[123].node.download_status[
            8
        ] = DownloadStatus.SuccessDownload
        app.chat_download_config[123].finish_task += 1
        # download success
        app.chat_download_config[123].node.download_status[
            10
        ] = DownloadStatus.SuccessDownload
        app.chat_download_config[123].finish_task += 1
        # not exist message
        app.chat_download_config[123].node.download_status[
            13
        ] = DownloadStatus.SuccessDownload
        app.config["chat"] = [{"chat_id": 123, "last_read_message_id": 5}]

        app.update_config(False)

        self.assertEqual(
            app.chat_download_config[123].last_read_message_id + 1,
            app.config["chat"][0]["last_read_message_id"],
        )
        self.assertEqual(
            [6, 7],
            app.app_data["chat"][0]["ids_to_retry"],
        )

    def test_update_config(self):
        with tempfile.TemporaryDirectory() as workdir:
            config_path = os.path.join(workdir, "config_test.yaml")
            data_path = os.path.join(workdir, "data_test.yaml")

            app = Application("config_test.yaml", "data_test.yaml")
            app.config_file = config_path
            app.app_data_file = data_path
            app.chat_download_config[123] = ChatDownloadConfig()
            app.config["chat"] = [{"chat_id": 123, "last_read_message_id": 0}]
            app.update_config()

            self.assertTrue(os.path.exists(config_path))
            self.assertTrue(os.path.exists(data_path))

            with open(data_path, encoding="utf-8") as data_file:
                data = yaml.YAML().load(data_file.read())
            self.assertEqual(data["chat"][0]["chat_id"], 123)

    def test_file_formats_are_normalized_to_lists(self):
        app = Application("", "")
        app.assign_config(
            {
                "api_id": 1,
                "api_hash": "hash",
                "media_types": ["video"],
                "file_formats": {
                    # A string used to make _can_download filter everything out.
                    "audio": "['all']",
                    "video": "mp4, mkv",
                    "document": ["pdf"],
                },
            }
        )

        self.assertEqual(app.file_formats["audio"], ["all"])
        self.assertEqual(app.file_formats["video"], ["mp4", "mkv"])
        self.assertEqual(app.file_formats["document"], ["pdf"])

    def test_file_formats_defaults_for_missing_keys(self):
        app = Application("", "")
        app.assign_config(
            {
                "api_id": 1,
                "api_hash": "hash",
                "media_types": ["video"],
                "file_formats": {},
            }
        )

        self.assertEqual(app.file_formats["video"], ["all"])
        self.assertEqual(app.file_formats["audio"], ["all"])

    def test_chat_with_empty_id_is_skipped(self):
        app = Application("", "")
        app.assign_config(
            {
                "api_id": 1,
                "api_hash": "hash",
                "media_types": ["video"],
                "file_formats": {"video": ["all"]},
                "chat": [
                    {"chat_id": None, "last_read_message_id": None},
                    {"chat_id": "-100123", "last_read_message_id": None},
                ],
            }
        )

        self.assertNotIn(None, app.chat_download_config)
        self.assertIn("-100123", app.chat_download_config)
        # A missing last_read_message_id must not become None (TypeError later).
        self.assertEqual(
            app.chat_download_config["-100123"].last_read_message_id, 0
        )

    def test_reload_hot_config(self):
        app = Application("", "")
        applied, restart = app.reload_hot_config(
            {
                "media_types": ["video"],
                "max_download_task": 3,
                "api_id": "1",
            }
        )
        self.assertIn("media_types", applied)
        self.assertEqual(app.media_types, ["video"])
        # Worker count is fixed at startup, so it must be reported, not applied.
        self.assertIn("max_download_task", restart)
        self.assertIn("api_id", restart)

    def test_media_types_string_is_normalized(self):
        # The raw YAML editor can save `media_types: video`; iterating that
        # string downloads nothing at all.
        app = Application("", "")
        app.assign_config(
            {"api_id": 1, "api_hash": "hash", "media_types": "video"}
        )
        self.assertEqual(app.media_types, ["video"])

    def test_unknown_media_types_fall_back_to_all(self):
        app = Application("", "")
        self.assertEqual(
            app._normalize_media_types(["nope"]),  # pylint: disable = W0212
            list(Application.MEDIA_TYPES),
        )
        self.assertEqual(
            app._normalize_media_types(["video", "nope"]),  # pylint: disable = W0212
            ["video"],
        )

    def test_web_login_secret_cannot_be_cleared_at_runtime(self):
        # Clearing the password while the listener stays bound to 0.0.0.0 would
        # expose an unauthenticated UI; it has to wait for a restart.
        app = Application("", "")
        app.web_login_secret = "s3cret"
        applied, restart = app.reload_hot_config({"web_login_secret": ""})

        self.assertNotIn("web_login_secret", applied)
        self.assertIn("web_login_secret", restart)
        self.assertEqual(app.web_login_secret, "s3cret")

    def test_web_login_secret_can_be_changed_at_runtime(self):
        app = Application("", "")
        app.web_login_secret = "s3cret"
        applied, _ = app.reload_hot_config({"web_login_secret": "new-secret"})

        self.assertIn("web_login_secret", applied)
        self.assertEqual(app.web_login_secret, "new-secret")

    def test_missing_config_is_created_from_the_template(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            template_path = os.path.join(tmp_dir, "config.yaml.example")
            with open(template_path, "w", encoding="utf-8") as template_file:
                template_file.write("api_id: your_api_id\n")
            config_path = os.path.join(tmp_dir, "config.yaml")

            Application._ensure_config_file(  # pylint: disable = W0212
                config_path
            )

            self.assertTrue(os.path.exists(config_path))
            with open(config_path, encoding="utf-8") as config_file:
                self.assertIn("your_api_id", config_file.read())

    def test_config_path_that_is_a_directory_is_reported_clearly(self):
        """Docker creates a directory when a bind-mount source is missing."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = os.path.join(tmp_dir, "config.yaml")
            os.makedirs(config_path)

            with self.assertRaises(IsADirectoryError) as caught:
                Application._ensure_config_file(  # pylint: disable = W0212
                    config_path
                )

            # The message has to tell the operator what to do, not just fail.
            self.assertIn("config.yaml.example", str(caught.exception))

    def test_caption_cache_is_bounded_per_chat(self):
        # One entry per album ever seen would grow for the whole run.
        app = Application.__new__(Application)
        app.caption_name_dict = {}
        app.caption_entities_dict = {}
        limit = module.app.MAX_CAPTION_CACHE_GROUPS

        for index in range(limit + 10):
            app.set_caption_name(-100, f"group-{index}", f"caption-{index}")
            app.set_caption_entities(-100, f"group-{index}", [index])

        self.assertEqual(len(app.caption_name_dict[-100]), limit)
        self.assertEqual(len(app.caption_entities_dict[-100]), limit)

        newest = limit + 9
        self.assertEqual(
            app.get_caption_name(-100, f"group-{newest}"),
            f"caption-{newest}",
        )
        self.assertEqual(
            app.get_caption_entities(-100, f"group-{newest}"), [newest]
        )
        # The oldest group was evicted.
        self.assertIsNone(app.get_caption_name(-100, "group-0"))
        self.assertIsNone(app.get_caption_entities(-100, "group-0"))


class UploadDriveBooleanConfigTestCase(unittest.TestCase):
    """An explicitly written ``false`` must beat the built-in default.

    ``after_upload_file_delete`` defaults to True, so the old truthiness check
    (`if upload_drive_config.get(...)`) silently deleted the local file even
    when the operator had turned that off.
    """

    BASE = {
        "api_id": 1,
        "api_hash": "hash",
        "media_types": ["video"],
    }

    def _app(self, upload_drive):
        app = Application("", "")
        config = dict(self.BASE)
        config["upload_drive"] = upload_drive
        app.assign_config(config)
        return app

    def test_after_upload_file_delete_false_is_honoured(self):
        app = self._app({"after_upload_file_delete": False})
        self.assertFalse(app.cloud_drive_config.after_upload_file_delete)

    def test_after_upload_file_delete_true_is_honoured(self):
        app = self._app({"after_upload_file_delete": True})
        self.assertTrue(app.cloud_drive_config.after_upload_file_delete)

    def test_defaults_are_kept_when_the_keys_are_absent(self):
        app = self._app({"enable_upload_file": True})
        self.assertTrue(app.cloud_drive_config.after_upload_file_delete)
        self.assertFalse(app.cloud_drive_config.before_upload_file_zip)
        self.assertTrue(app.cloud_drive_config.hash_check)

    def test_enable_upload_file_false_is_honoured(self):
        app = self._app({"enable_upload_file": False, "after_upload_file_delete": True})
        self.assertFalse(app.cloud_drive_config.enable_upload_file)

    def test_hot_reload_applies_false(self):
        app = self._app({})
        app.reload_hot_config(
            {"upload_drive": {"after_upload_file_delete": False, "hash_check": False}}
        )
        self.assertFalse(app.cloud_drive_config.after_upload_file_delete)
        self.assertFalse(app.cloud_drive_config.hash_check)


class FilePathPrefixTestCase(unittest.TestCase):
    """The folder is named after the chat title, which is not unique."""

    def test_chat_id_prefix_keeps_two_same_titled_chats_apart(self):
        app = Application("", "")
        app.save_path = os.path.join(os.sep, "downloads")
        app.file_path_prefix = ["chat_title", "chat_id", "media_datetime"]

        first = app.get_file_save_path("video", "同名频道", "2026_11", chat_id=-1001)
        second = app.get_file_save_path("video", "同名频道", "2026_11", chat_id=-1002)

        self.assertNotEqual(first, second)
        self.assertIn("-1001", first)
        self.assertIn("-1002", second)

    def test_chat_id_prefix_is_optional(self):
        app = Application("", "")
        app.save_path = os.path.join(os.sep, "downloads")
        app.file_path_prefix = ["chat_title", "chat_id"]

        # Callers without a chat id (older code paths) keep working unchanged.
        self.assertEqual(
            app.get_file_save_path("video", "chan", "2026_11"),
            os.path.join(os.sep, "downloads", "chan"),
        )
