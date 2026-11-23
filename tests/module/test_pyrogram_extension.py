"""Regression tests for upload failure handling and download status caching."""

import unittest
from types import SimpleNamespace
from unittest import mock

from pyrogram import enums, raw, types

from module.app import DownloadStatus, ForwardStatus, TaskNode, UploadStatus
from module.pyrogram_extension import (
    _download_cache,
    convert_message_entity,
    proc_cache_forward,
    record_download_status,
    truncate_caption,
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
        with (
            mock.patch(
                "module.pyrogram_extension.upload_telegram_chat_message",
                new=mock.AsyncMock(return_value=ForwardStatus.FailedForward),
            ),
            mock.patch("module.pyrogram_extension.os.remove") as remove_mock,
        ):
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
        with (
            mock.patch(
                "module.pyrogram_extension.upload_telegram_chat_message",
                new=mock.AsyncMock(return_value=ForwardStatus.SuccessForward),
            ),
            mock.patch("module.pyrogram_extension.os.remove") as remove_mock,
        ):
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

        self.assertIs(_download_cache[(-100999, 987654)], DownloadStatus.FailedDownload)


class CaptionEntityAndCacheForwardTestCase(unittest.IsolatedAsyncioTestCase):
    def test_truncate_caption_uses_utf16_length_for_entities(self):
        # "😀" takes 1 codepoint in Python but 2 code units in UTF-16.
        text = "😀abcdeXYZ"
        entities = [
            types.MessageEntity(
                type=enums.MessageEntityType.BOLD, offset=6, length=1
            ),  # covers 'e' at UTF-16 offset 6..7
            types.MessageEntity(
                type=enums.MessageEntityType.ITALIC, offset=2, length=8
            ),  # spans past limit=7 -> clipped to length=5
            types.MessageEntity(
                type=enums.MessageEntityType.CODE, offset=7, length=2
            ),  # past limit=7 -> dropped
        ]

        truncated_text, truncated_entities = truncate_caption(text, entities, 7)

        self.assertEqual(truncated_text, "😀abcde")
        self.assertEqual(len(truncated_entities), 2)
        self.assertEqual(
            (truncated_entities[0].offset, truncated_entities[0].length), (6, 1)
        )
        self.assertEqual(
            (truncated_entities[1].offset, truncated_entities[1].length), (2, 5)
        )

    def test_convert_message_entity_only_sets_user_when_present(self):
        bold = convert_message_entity(
            None, raw.types.MessageEntityBold(offset=0, length=4)
        )
        self.assertIsNone(bold.user)

        mention = convert_message_entity(
            None,
            raw.types.InputMessageEntityMentionName(
                offset=0,
                length=4,
                user_id=raw.types.InputUser(user_id=42, access_hash=0),
            ),
        )
        self.assertIsNotNone(mention.user)
        self.assertEqual(mention.user.id, 42)

    async def test_proc_cache_forward_skips_skip_upload_and_empty_group(self):
        app = SimpleNamespace(hide_file_name=False)
        client = mock.Mock()
        node = TaskNode(chat_id=-1001, upload_telegram_chat_id=-1002)
        node.media_group_ids["grp1"] = {1: mock.Mock(message="cap"), 2: None}
        node.download_status = {
            1: DownloadStatus.SuccessDownload,
            2: DownloadStatus.SuccessDownload,
        }
        node.upload_status = {
            1: UploadStatus.SuccessUpload,
            2: UploadStatus.SkipUpload,
        }
        msg = SimpleNamespace(id=1, media_group_id="grp1")

        with mock.patch(
            "module.pyrogram_extension.send_media_group_v2",
            new=mock.AsyncMock(return_value=True),
        ) as send_mock:
            status = await proc_cache_forward(client, node, msg, True, app)

        self.assertEqual(status, ForwardStatus.CacheForward)
        send_mock.assert_awaited_once()
        self.assertEqual(len(send_mock.await_args.args[2]), 1)
        self.assertNotIn("grp1", node.media_group_ids)

        # When every item is skipped, return SkipForward without sending an empty album.
        node.media_group_ids["grp2"] = {10: None}
        node.download_status[10] = DownloadStatus.SuccessDownload
        node.upload_status[10] = UploadStatus.SkipUpload
        msg2 = SimpleNamespace(id=10, media_group_id="grp2")

        with mock.patch(
            "module.pyrogram_extension.send_media_group_v2",
            new=mock.AsyncMock(return_value=True),
        ) as send_empty_mock:
            status2 = await proc_cache_forward(client, node, msg2, True, app)

        self.assertEqual(status2, ForwardStatus.SkipForward)
        send_empty_mock.assert_not_awaited()
        self.assertNotIn("grp2", node.media_group_ids)
