"""test app"""

import os
import sys
import tempfile
import unittest
from unittest import mock

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
