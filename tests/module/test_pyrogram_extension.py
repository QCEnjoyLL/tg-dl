"""Regression tests for upload failure handling and download status caching."""

import unittest
from unittest import mock

from module.app import DownloadStatus, ForwardStatus
from module.pyrogram_extension import (
    _download_cache,
    record_download_status,
    upload_telegram_chat,
)


class UploadTelegramChatTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = mock.Mock()
        self.app.after_upload_telegram_delete = True
        self.app.hide_file_name = False
        self.node = mock.Mock()
        self.node.upload_telegram_chat_id = -100123
        self.node.upload_user = None
        self.node.stat_forward = mock.Mock()
        self.message = mock.Mock()
        self.message.media = None
        self.client = mock.Mock()
        self.file_name = "/tmp/keep-me.mp4"

    async def test_failed_upload_keeps_local_file(self):
        with mock.patch(
            "module.pyrogram_extension.upload_telegram_chat_message",
            new=mock.AsyncMock(return_value=ForwardStatus.FailedForward),
        ), mock.patch("module.pyrogram_extension.os.remove") as remove_mock:
            await upload_telegram_chat(
                self.client,
                self.client,
                self.app,
                self.node,
                self.message,
                DownloadStatus.SuccessDownload,
                self.file_name,
            )

        remove_mock.assert_not_called()

    async def test_successful_upload_deletes_local_file(self):
        with mock.patch(
            "module.pyrogram_extension.upload_telegram_chat_message",
            new=mock.AsyncMock(return_value=ForwardStatus.SuccessForward),
        ), mock.patch("module.pyrogram_extension.os.remove") as remove_mock:
            await upload_telegram_chat(
                self.client,
                self.client,
                self.app,
                self.node,
                self.message,
                DownloadStatus.SuccessDownload,
                self.file_name,
            )

        remove_mock.assert_called_once_with(self.file_name)


class RecordDownloadStatusTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_exception_does_not_leave_cache_in_downloading_state(self):
        class BoomError(Exception):
            pass

        @record_download_status
        async def failing(_client, _message, _types, _formats, _node):
            raise BoomError()

        node = mock.Mock()
        node.chat_id = -100999
        message = mock.Mock()
        message.id = 987654

        with self.assertRaises(BoomError):
            await failing(None, message, None, None, node)

        self.assertIs(
            _download_cache[(-100999, 987654)], DownloadStatus.FailedDownload
        )
