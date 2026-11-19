"""Regression tests for the shared download state and the history store."""

import os
import tempfile
import unittest

from module import download_stat
from module.download_stat import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    clear_completed_downloads,
    clear_failed_downloads,
    count_completed_downloads,
    count_failed_downloads,
    delete_task,
    get_completed_downloads,
    get_download_result,
    get_failed_downloads,
    init_history_db,
    close_history_db,
    is_task_cancelled,
    is_task_paused,
    mark_download_completed,
    mark_download_failed,
    mark_download_retried,
    normalize_chat_id,
    query_completed_downloads,
    query_failed_downloads,
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
        # A fresh in-memory database per test; the module keeps one connection.
        close_history_db()
        init_history_db(None)
        with download_stat._PROGRESS_LOCK:  # pylint: disable = W0212
            download_stat._download_result.clear()
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

    def test_completed_history_is_not_capped(self):
        for index in range(1200):
            mark_download_completed(-100, index, f"/tmp/{index}.mp4", 1)

        self.assertEqual(count_completed_downloads(), 1200)
        rows = get_completed_downloads()
        self.assertEqual(len(rows), 1200)
        # Newest first, nothing evicted.
        self.assertEqual(rows[0]["id"], 1199)
        self.assertEqual(rows[-1]["id"], 0)

    def test_failed_history_is_not_capped(self):
        for index in range(700):
            mark_download_failed(-100, index, f"/tmp/{index}.mp4", "boom")

        self.assertEqual(count_failed_downloads(), 700)
        self.assertEqual(len(get_failed_downloads()[-100]), 700)

    def test_failed_row_is_removed_from_progress(self):
        with download_stat._PROGRESS_LOCK:  # pylint: disable = W0212
            download_stat._download_result[-100] = {7: {"down_byte": 1}}

        remove_download_result(-100, 7)
        mark_download_failed(-100, 7, "/tmp/x.mp4", "boom")

        self.assertEqual(get_download_result(), {})
        self.assertEqual(get_failed_downloads()[-100][7]["reason"], "boom")

    def test_retry_removes_the_failure(self):
        mark_download_failed(-100, 7, "/tmp/x.mp4", "boom")
        mark_download_retried(-100, 7)

        self.assertEqual(get_failed_downloads(), {})
        self.assertEqual(count_failed_downloads(), 0)

    def test_completing_clears_the_previous_failure(self):
        # A message that failed once and succeeded on retry must not stay in
        # both the failed and the completed history.
        mark_download_failed(-100, 7, "/tmp/x.mp4", "boom")
        mark_download_completed(-100, 7, "/tmp/x.mp4", 20, "uploaded")

        self.assertEqual(get_failed_downloads(), {})
        self.assertEqual(count_completed_downloads(), 1)

    def test_completed_query_filters_and_paginates(self):
        mark_download_completed(-100, 1, "/tmp/a.mp4", 10, "uploaded")
        mark_download_completed(-100, 2, "/tmp/b.mov", 20, "exists")
        mark_download_completed(-200, 3, "/tmp/c.zip", 30, None)

        uploaded = query_completed_downloads(upload_state="uploaded")
        self.assertEqual(uploaded["total"], 1)
        self.assertEqual(uploaded["rows"][0]["id"], 1)

        exists = query_completed_downloads(upload_state="exists")
        self.assertEqual(exists["total"], 1)
        self.assertEqual(exists["rows"][0]["id"], 2)

        local = query_completed_downloads(upload_state="local")
        self.assertEqual(local["total"], 1)
        self.assertEqual(local["rows"][0]["id"], 3)

        by_name = query_completed_downloads(q="b.mov")
        self.assertEqual(by_name["total"], 1)
        self.assertEqual(by_name["rows"][0]["id"], 2)

        by_chat = query_completed_downloads(q="-200")
        self.assertEqual(by_chat["total"], 1)
        self.assertEqual(by_chat["rows"][0]["id"], 3)

        page_two = query_completed_downloads(page=2, page_size=2)
        self.assertEqual(page_two["total"], 3)
        self.assertEqual(page_two["pages"], 2)
        self.assertEqual(page_two["page"], 2)
        self.assertEqual([row["id"] for row in page_two["rows"]], [1])

        # Out-of-range pages clamp instead of returning an empty table.
        clamped = query_completed_downloads(page=99, page_size=2)
        self.assertEqual(clamped["page"], 2)

        # page_size=0 means "as much as one response allows".
        everything = query_completed_downloads(page_size=0)
        self.assertEqual(everything["page_size"], MAX_PAGE_SIZE)

        default_page = query_completed_downloads()
        self.assertEqual(default_page["page_size"], DEFAULT_PAGE_SIZE)

    def test_failed_query_filters_and_paginates(self):
        mark_download_failed(-100, 1, "/tmp/a.mp4", "file reference expired")
        mark_download_failed(-100, 2, "/tmp/b.mp4", "FloodWait")
        mark_download_failed(-200, 3, "/tmp/c.mp4", "[-500 TIMEOUT]")

        by_reason = query_failed_downloads(q="floodwait")
        self.assertEqual(by_reason["total"], 1)
        self.assertEqual(by_reason["rows"][0]["id"], 2)

        by_chat = query_failed_downloads(q="-200")
        self.assertEqual(by_chat["total"], 1)
        self.assertEqual(by_chat["rows"][0]["id"], 3)

        paged = query_failed_downloads(page=1, page_size=2)
        self.assertEqual(paged["total"], 3)
        self.assertEqual(paged["pages"], 2)
        self.assertEqual(len(paged["rows"]), 2)
        self.assertEqual(paged["all_total"], 3)

    def test_clear_completed_and_failed(self):
        mark_download_completed(-100, 1, "/tmp/a.mp4", 10)
        mark_download_completed(-200, 2, "/tmp/b.mp4", 10)
        mark_download_failed(-100, 3, "/tmp/c.mp4", "boom")

        self.assertEqual(clear_completed_downloads(-100), 1)
        self.assertEqual(count_completed_downloads(), 1)
        self.assertEqual(clear_completed_downloads(), 1)
        self.assertEqual(count_completed_downloads(), 0)

        self.assertEqual(clear_failed_downloads(-100), 1)
        self.assertEqual(count_failed_downloads(), 0)

    def test_get_completed_download_and_update_upload_state(self):
        mark_download_completed(-100, 7, "/tmp/x.mp4", 20)

        record = download_stat.get_completed_download(-100, 7)
        self.assertEqual(record["file_name"], "/tmp/x.mp4")
        self.assertIsNone(record["upload_state"])

        self.assertTrue(
            download_stat.update_completed_upload_state(-100, 7, "uploaded")
        )
        record = download_stat.get_completed_download("-100", 7)
        self.assertEqual(record["upload_state"], "uploaded")
        self.assertTrue(record["upload_success"])

        # Unknown rows and ids report failure instead of raising.
        self.assertFalse(
            download_stat.update_completed_upload_state(-100, 999, "uploaded")
        )
        self.assertIsNone(download_stat.get_completed_download(-100, 999))

    def test_history_survives_reopening_the_database(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "history.db")
            try:
                init_history_db(db_path)
                mark_download_completed(-100, 5, "/tmp/a.mp4", 10, "uploaded")
                mark_download_failed(-100, 6, "/tmp/b.mp4", "boom")
                close_history_db()

                init_history_db(db_path)

                completed = get_completed_downloads()
                self.assertEqual(len(completed), 1)
                self.assertEqual(completed[0]["id"], 5)
                self.assertEqual(completed[0]["upload_state"], "uploaded")
                self.assertEqual(get_failed_downloads()[-100][6]["reason"], "boom")
            finally:
                close_history_db()
                self._clear()

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

    def test_delete_accepts_text_chat_ids(self):
        # The Web UI sends chat_id as text; the stored keys must still match.
        mark_download_failed(-100, 11, "/tmp/bad.mp4")
        self.assertTrue(delete_task("-100", 11))
        self.assertEqual(get_failed_downloads(), {})

        with download_stat._PROGRESS_LOCK:  # pylint: disable = W0212
            download_stat._download_result[-100] = {12: {"down_byte": 1}}
        self.assertTrue(delete_task("-100", 12))
        self.assertTrue(is_task_cancelled(-100, 12))
        self.assertEqual(get_download_result(), {})
        download_stat.clear_task_cancelled(-100, 12)

    def test_normalize_chat_id(self):
        self.assertEqual(normalize_chat_id("-100123"), -100123)
        self.assertEqual(normalize_chat_id(-100123), -100123)
        self.assertEqual(normalize_chat_id("@mychannel"), "@mychannel")

    def test_total_speed_is_the_sum_of_the_rows(self):
        # The header used to come from a separate global counter that drifted
        # from the table (0 B/s in the header while rows showed ~60 KB/s).
        with download_stat._PROGRESS_LOCK:  # pylint: disable = W0212
            download_stat._download_result[-100] = {
                1: {"down_byte": 10, "download_speed": 60000},
                2: {"down_byte": 20, "download_speed": 60000},
            }
            download_stat._download_result[-200] = {
                3: {"down_byte": 30, "download_speed": 30000},
            }

        self.assertEqual(download_stat.get_total_download_speed(), 150000)

    def test_total_speed_is_zero_without_rows(self):
        self.assertEqual(download_stat.get_total_download_speed(), 0)


if __name__ == "__main__":
    unittest.main()
