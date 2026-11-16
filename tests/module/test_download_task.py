"""Regression tests for terminal bookkeeping in download_task."""

import unittest
from unittest import mock

import media_downloader as md
from module.app import DownloadStatus


class DownloadTaskFinalizeTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_exception_still_records_failure_and_finishes_task(self):
        node = mock.Mock()
        node.chat_id = -100123
        node.bot = None
        node.upload_user = None
        node.upload_telegram_chat_id = None
        node.download_status = {}
        node.last_error = {}
        message = mock.Mock()
        message.id = 42
        message.text = None
        message.media = None

        with mock.patch.object(
            md, "download_media", new=mock.AsyncMock(side_effect=RuntimeError("boom"))
        ), mock.patch.object(md, "remove_download_result") as remove_mock, mock.patch.object(
            md, "mark_download_failed"
        ) as mark_mock, mock.patch.object(
            md, "report_bot_download_status", new=mock.AsyncMock()
        ) as report_mock, mock.patch.object(
            md.app, "set_download_id"
        ) as set_id_mock:
            await md.download_task(mock.Mock(), message, node)

        self.assertIs(node.download_status[42], DownloadStatus.FailedDownload)
        remove_mock.assert_called_once_with(-100123, 42)
        mark_mock.assert_called_once_with(-100123, 42, "", "")
        set_id_mock.assert_called_once_with(node, 42, DownloadStatus.FailedDownload)
        report_mock.assert_awaited_once_with(
            None, node, DownloadStatus.FailedDownload, 0
        )

    async def test_upload_exception_does_not_hide_successful_download(self):
        node = mock.Mock()
        node.chat_id = -100123
        node.bot = None
        node.upload_user = None
        node.upload_telegram_chat_id = -100456
        node.download_status = {}
        message = mock.Mock()
        message.id = 43
        message.text = None
        message.media = mock.Mock()

        with mock.patch.object(
            md,
            "download_media",
            new=mock.AsyncMock(
                return_value=(DownloadStatus.SuccessDownload, "/tmp/finished.mp4")
            ),
        ), mock.patch.object(
            md, "upload_telegram_chat", new=mock.AsyncMock(side_effect=RuntimeError("up"))
        ), mock.patch.object(
            md, "report_bot_download_status", new=mock.AsyncMock()
        ), mock.patch.object(
            md, "mark_download_completed"
        ) as completed_mock, mock.patch.object(
            md.app, "set_download_id"
        ), mock.patch.object(
            md.os.path, "getsize", return_value=123
        ):
            await md.download_task(mock.Mock(), message, node)

        # The download itself finished, so the message must not be re-downloaded
        # as a failure just because an upload step raised.
        self.assertIs(node.download_status[43], DownloadStatus.SuccessDownload)
        # ... and it must leave the in-progress list, otherwise the Web UI keeps
        # showing a finished task at 100% forever.
        completed_mock.assert_called_once_with(
            -100123, 43, "/tmp/finished.mp4", 123, None
        )

    async def test_cloud_upload_state_is_recorded(self):
        from module.cloud_drive import UPLOAD_EXISTS

        node = mock.Mock()
        node.chat_id = -100123
        node.bot = None
        node.upload_user = None
        node.upload_telegram_chat_id = None
        node.upload_success_count = 0
        node.upload_skipped_count = 0
        node.download_status = {}
        message = mock.Mock()
        message.id = 44
        message.text = None
        message.media = mock.Mock()

        with mock.patch.object(
            md,
            "download_media",
            new=mock.AsyncMock(
                return_value=(DownloadStatus.SuccessDownload, "/tmp/cloud.mp4")
            ),
        ), mock.patch.object(
            md, "upload_telegram_chat", new=mock.AsyncMock()
        ), mock.patch.object(
            md.app,
            "upload_file",
            new=mock.AsyncMock(return_value=UPLOAD_EXISTS),
        ), mock.patch.object(
            md, "report_bot_download_status", new=mock.AsyncMock()
        ), mock.patch.object(
            md, "mark_download_completed"
        ) as completed_mock, mock.patch.object(
            md.app, "set_download_id"
        ), mock.patch.object(
            md.os.path, "getsize", return_value=10
        ):
            await md.download_task(mock.Mock(), message, node)

        self.assertEqual(node.upload_success_count, 1)
        self.assertEqual(node.upload_skipped_count, 1)
        completed_mock.assert_called_once_with(
            -100123, 44, "/tmp/cloud.mp4", 10, UPLOAD_EXISTS
        )
