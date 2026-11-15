"""Regression tests for the shared download/upload progress state."""

import unittest

from module import download_stat
from module.download_stat import (
    MAX_COMPLETED_DOWNLOADS,
    delete_task,
    get_completed_downloads,
    get_failed_downloads,
    get_download_result,
    is_task_cancelled,
    is_task_paused,
    mark_download_completed,
    mark_download_failed,
    remove_download_result,
    set_task_paused,
)


class DownloadStatTestCase(unittest.TestCase):
    def setUp(self):
        self._clear()

    def tearDown(self):
        self._clear()

    @staticmethod
    def _clear():
        with download_stat._PROGRESS_LOCK:  # pylint: disable = W0212
            download_stat._download_result.clear()
            download_stat._completed_downloads.clear()
            download_stat._failed_downloads.clear()
            download_stat._paused_tasks.clear()
            download_stat._cancelled_tasks.clear()

    def test_snapshot_is_a_copy(self):
        with download_stat._PROGRESS_LOCK:  # pylint: disable = W0212
            download_stat._download_result[-100] = {1: {"down_byte": 5}}

        snapshot = get_download_result()
        snapshot[-100][1]["down_byte"] = 999

        self.assertEqual(get_download_result()[-100][1]["down_byte"], 5)

    def test_completed_moves_row_out_of_progress(self):
        with download_stat._PROGRESS_LOCK:  # pylint: disable = W0212
            download_stat._download_result[-100] = {
                7: {"down_byte": 10, "total_size": 20, "file_name": "/tmp/x.mp4"}
            }

        mark_download_completed(-100, 7, "/tmp/x.mp4", 20, "uploaded")

        self.assertEqual(get_download_result(), {})
        completed = get_completed_downloads()
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["id"], 7)
        self.assertEqual(completed[0]["down_byte"], 20)
        self.assertTrue(completed[0]["upload_success"])
        self.assertEqual(completed[0]["upload_state"], "uploaded")
        # Synchronous success must not be reported as a failure.
        self.assertEqual(get_failed_downloads(), {})

    def test_completed_list_is_bounded(self):
        for index in range(MAX_COMPLETED_DOWNLOADS + 25):
            mark_download_completed(-100, index, f"/tmp/{index}.mp4", 1)

        self.assertEqual(len(get_completed_downloads()), MAX_COMPLETED_DOWNLOADS)
        # Newest first, oldest evicted.
        self.assertEqual(get_completed_downloads()[0]["id"], MAX_COMPLETED_DOWNLOADS + 24)

    def test_failed_row_is_removed_from_progress(self):
        with download_stat._PROGRESS_LOCK:  # pylint: disable = W0212
            download_stat._download_result[-100] = {7: {"down_byte": 1}}

        remove_download_result(-100, 7)
        mark_download_failed(-100, 7, "/tmp/x.mp4", "boom")

        self.assertEqual(get_download_result(), {})
        self.assertEqual(get_failed_downloads()[-100][7]["reason"], "boom")

    def test_pause_and_resume_are_per_task(self):
        self.assertFalse(is_task_paused(-100, 7))
        self.assertTrue(set_task_paused(-100, 7, True))
        self.assertTrue(is_task_paused("-100", 7))
        self.assertFalse(is_task_paused(-100, 8))
        self.assertTrue(set_task_paused(-100, 7, False))
        self.assertFalse(is_task_paused(-100, 7))

    def test_delete_downloading_task_arms_cancel(self):
        with download_stat._PROGRESS_LOCK:  # pylint: disable = W0212
            download_stat._download_result[-100] = {7: {"down_byte": 1}}

        self.assertTrue(delete_task(-100, 7))
        self.assertTrue(is_task_cancelled(-100, 7))
        self.assertEqual(get_download_result(), {})

        download_stat.clear_task_cancelled(-100, 7)
        self.assertFalse(is_task_cancelled(-100, 7))

    def test_delete_completed_or_failed_task_does_not_cancel(self):
        mark_download_completed(-100, 9, "/tmp/done.mp4", 10)
        self.assertTrue(delete_task(-100, 9))
        self.assertEqual(get_completed_downloads(), [])
        self.assertFalse(is_task_cancelled(-100, 9))

        mark_download_failed(-100, 10, "/tmp/bad.mp4")
        self.assertTrue(delete_task(-100, 10))
        self.assertEqual(get_failed_downloads(), {})
        self.assertFalse(is_task_cancelled(-100, 10))
