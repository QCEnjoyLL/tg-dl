"""Regression tests for terminal bookkeeping in download_task."""

import unittest
from unittest import mock

import media_downloader as md
from module import upload_stat
from module.app import DownloadStatus, TaskNode


class DownloadTaskFinalizeTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        upload_stat.clear()

    def tearDown(self):
        upload_stat.clear()

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

    async def test_cloud_upload_is_delegated_to_the_background_dispatcher(self):
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

        dispatcher = mock.Mock()
        with mock.patch.object(
            md.app.cloud_drive_config, "enable_upload_file", True
        ), mock.patch.object(
            md,
            "download_media",
            new=mock.AsyncMock(
                return_value=(DownloadStatus.SuccessDownload, "/tmp/cloud.mp4")
            ),
        ), mock.patch.object(
            md, "upload_telegram_chat", new=mock.AsyncMock()
        ), mock.patch.object(
            md, "upload_dispatcher", dispatcher
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

        # The download row is recorded as "仅本地" first; the upload job fills in
        # the state later, so the worker can fetch the next message right away.
        completed_mock.assert_called_once_with(
            -100123, 44, "/tmp/cloud.mp4", 10, None
        )
        dispatcher.submit.assert_called_once()

    async def test_background_upload_records_the_upload_state(self):
        from module.cloud_drive import UPLOAD_EXISTS

        node = mock.Mock()
        node.chat_id = -100123
        node.upload_success_count = 0
        node.upload_skipped_count = 0

        with mock.patch.object(
            md.app, "upload_file", new=mock.AsyncMock(return_value=UPLOAD_EXISTS)
        ), mock.patch.object(
            md, "update_completed_upload_state"
        ) as update_mock, mock.patch.object(
            md.upload_stat, "remove"
        ) as remove_mock:
            await md._run_cloud_upload(  # pylint: disable = W0212
                node, 44, "/tmp/cloud.mp4", "cloud.mp4"
            )

        self.assertEqual(node.upload_success_count, 1)
        self.assertEqual(node.upload_skipped_count, 1)
        update_mock.assert_called_once_with(-100123, 44, UPLOAD_EXISTS)
        remove_mock.assert_called_once_with(-100123, 44)

    async def test_background_upload_failure_keeps_the_row_local(self):
        node = mock.Mock()
        node.chat_id = -100123
        node.upload_success_count = 0
        node.upload_skipped_count = 0

        with mock.patch.object(
            md.app, "upload_file", new=mock.AsyncMock(return_value=None)
        ), mock.patch.object(
            md, "update_completed_upload_state"
        ) as update_mock:
            await md._run_cloud_upload(  # pylint: disable = W0212
                node, 44, "/tmp/cloud.mp4", "cloud.mp4"
            )

        # No state update: the row stays "仅本地" so the re-upload button works.
        update_mock.assert_not_called()
        self.assertEqual(node.upload_success_count, 0)

    async def test_background_upload_exception_is_contained(self):
        node = mock.Mock()
        node.chat_id = -100123

        with mock.patch.object(
            md.app,
            "upload_file",
            new=mock.AsyncMock(side_effect=RuntimeError("drive offline")),
        ), mock.patch.object(md, "update_completed_upload_state") as update_mock:
            await md._run_cloud_upload(  # pylint: disable = W0212
                node, 44, "/tmp/cloud.mp4", "cloud.mp4"
            )

        update_mock.assert_not_called()

    async def test_background_upload_skips_duplicate_active_upload(self):
        node = TaskNode(chat_id=-100123)
        upload_stat.try_start(-100123, 44, file_name="cloud.mp4")

        with mock.patch.object(
            md.app, "upload_file", new=mock.AsyncMock(return_value="uploaded")
        ) as upload_mock:
            await md._run_cloud_upload(  # pylint: disable = W0212
                node, 44, "/tmp/cloud.mp4", "cloud.mp4"
            )

        upload_mock.assert_not_awaited()
        # Rejecting the duplicate must leave the in-progress slot intact.
        self.assertTrue(upload_stat.is_uploading(-100123, 44))

    async def test_background_upload_updates_node_counters_and_bot_card(self):
        node = TaskNode(chat_id=-100123, reply_message_id=99, bot=mock.Mock())
        node.is_running = True
        node.total_task = 1
        node.total_download_task = 1
        node.pending_upload_count = 1
        node.cloud_drive_upload_stat_dict[44] = mock.Mock()

        with mock.patch.object(
            md.app, "upload_file", new=mock.AsyncMock(return_value="uploaded")
        ), mock.patch.object(md, "update_completed_upload_state"), mock.patch.object(
            md, "report_bot_status", new=mock.AsyncMock()
        ) as report_mock:
            await md._run_cloud_upload(  # pylint: disable = W0212
                node, 44, "/tmp/cloud.mp4", "cloud.mp4"
            )

        self.assertEqual(node.pending_upload_count, 0)
        self.assertNotIn(44, node.cloud_drive_upload_stat_dict)
        self.assertTrue(node.is_finish())
        report_mock.assert_awaited_once_with(node.bot, node, immediate_reply=True)
