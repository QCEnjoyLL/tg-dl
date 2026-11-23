"""Lifecycle tests for TaskNode: a task must not look finished while scanning."""

import unittest
from types import SimpleNamespace
from unittest import mock

import media_downloader as md
from module.app import ChatDownloadConfig, TaskNode


class TaskNodeScanningTestCase(unittest.TestCase):
    def test_scanning_task_is_not_finished(self):
        node = TaskNode(chat_id=-100)
        node.is_scanning = True

        self.assertFalse(node.is_finish())

    def test_scanning_blocks_finish_even_when_the_counts_match(self):
        node = TaskNode(chat_id=-100)
        node.is_scanning = True
        node.is_running = True

        self.assertEqual(node.total_task, node.total_download_task)
        self.assertFalse(node.is_finish())

    def test_task_finishes_once_the_scan_is_over(self):
        node = TaskNode(chat_id=-100)
        node.is_running = True

        self.assertTrue(node.is_finish())

    def test_running_task_with_pending_downloads_is_not_finished(self):
        node = TaskNode(chat_id=-100)
        node.is_running = True
        node.total_task = 3
        node.total_download_task = 1

        self.assertFalse(node.is_finish())

    def test_stopped_task_is_finished(self):
        node = TaskNode(chat_id=-100)
        node.stop_transmission()

        self.assertTrue(node.is_finish())

    def test_pending_background_uploads_block_finish(self):
        node = TaskNode(chat_id=-100)
        node.is_running = True
        node.total_task = 1
        node.total_download_task = 1
        node.pending_upload_count = 1

        self.assertFalse(node.is_finish())

        node.pending_upload_count = 0
        self.assertTrue(node.is_finish())


def make_message(message_id: int = 1):
    return SimpleNamespace(
        id=message_id,
        caption=None,
        media_group_id=None,
        caption_entities=None,
    )


class DownloadChatTaskScanningTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_task_is_marked_scanning_while_the_history_is_read(self):
        node = TaskNode(chat_id=-100123)
        config = ChatDownloadConfig()
        config.last_read_message_id = 1
        seen = {}

        async def history(*_args, **_kwargs):
            seen["during_scan"] = node.is_scanning
            yield make_message(1)

        async def add_task(_message, task_node):
            seen["in_loop"] = task_node.is_scanning
            task_node.total_task += 1
            return True

        with (
            mock.patch.object(md, "get_chat_history_v2", history),
            mock.patch.object(md, "add_download_task", add_task),
            mock.patch.object(md, "set_meta_data"),
            mock.patch.object(md, "MetaData"),
            mock.patch.object(md.app, "exec_filter", return_value=True),
            mock.patch.object(md.app, "need_skip_message", return_value=False),
            mock.patch.object(md.app, "set_caption_name"),
            mock.patch.object(md.app, "set_caption_entities"),
            mock.patch.object(md.app, "get_caption_name", return_value=""),
        ):
            await md.download_chat_task(mock.Mock(), config, node)

        self.assertTrue(seen["during_scan"])
        self.assertTrue(seen["in_loop"])
        self.assertFalse(node.is_scanning)
        self.assertTrue(node.is_running)

    async def test_scanning_ends_even_when_the_history_raises(self):
        node = TaskNode(chat_id=-100123)
        config = ChatDownloadConfig()

        async def history(*_args, **_kwargs):
            raise RuntimeError("boom")
            yield make_message()  # pragma: no cover

        with mock.patch.object(md, "get_chat_history_v2", history):
            with self.assertRaises(RuntimeError):
                await md.download_chat_task(mock.Mock(), config, node)

        # The task must not stay stuck in "scanning" forever.
        self.assertFalse(node.is_scanning)
        self.assertTrue(node.is_running)


if __name__ == "__main__":
    unittest.main()
