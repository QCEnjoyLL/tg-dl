"""Tests for the in-memory cloud upload progress store."""

import unittest

from module import upload_stat


class UploadStatTestCase(unittest.TestCase):
    def setUp(self):
        upload_stat.clear()

    def tearDown(self):
        upload_stat.clear()

    def test_update_exposes_a_row_with_parsed_speed(self):
        upload_stat.update(
            -100123,
            42,
            file_name="/tmp/a.mp4",
            transferred="1.5 MB",
            total="10 MB",
            percentage="15%",
            speed="2.5 MB/s",
            eta="4s",
        )

        rows = upload_stat.rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["chat"], -100123)
        self.assertEqual(row["id"], 42)
        self.assertEqual(row["file_name"], "/tmp/a.mp4")
        self.assertEqual(row["percentage"], 15.0)
        self.assertEqual(row["progress"], 15.0)
        self.assertEqual(row["speed"], "2.5 MB/s")
        self.assertEqual(row["speed_bytes"], int(2.5 * 1024 * 1024))
        self.assertEqual(row["eta"], "4s")
        self.assertEqual(upload_stat.count(), 1)
        self.assertEqual(upload_stat.total_speed(), int(2.5 * 1024 * 1024))

    def test_update_replaces_the_previous_line_of_the_same_message(self):
        upload_stat.update(-100123, 42, percentage="10%", speed="1 MB/s")
        upload_stat.update(-100123, 42, percentage="60%", speed="2 MB/s")

        self.assertEqual(upload_stat.count(), 1)
        self.assertEqual(upload_stat.rows()[0]["progress"], 60.0)

    def test_remove_and_snapshot(self):
        upload_stat.update(-100123, 42, percentage="10%")
        upload_stat.update(-100123, 43, percentage="20%")
        snapshot = upload_stat.snapshot()
        self.assertEqual(len(snapshot["-100123"]), 2)

        # The snapshot is a copy: mutating it must not touch the store.
        snapshot["-100123"][42]["progress"] = 99
        self.assertEqual(upload_stat.rows()[0]["progress"], 20.0)

        upload_stat.remove(-100123, 42)
        upload_stat.remove(-100123, 43)
        self.assertEqual(upload_stat.count(), 0)
        self.assertEqual(upload_stat.snapshot(), {})

    def test_total_speed_is_zero_without_rows(self):
        self.assertEqual(upload_stat.total_speed(), 0)

    def test_try_start_and_is_uploading_deduplicate_across_str_and_int(self):
        self.assertFalse(upload_stat.is_uploading(-100123, 42))
        self.assertTrue(upload_stat.try_start(-100123, 42, file_name="a.mp4"))
        self.assertTrue(upload_stat.is_uploading("-100123", 42))
        # Second claim on the same message (even as str chat id) is rejected.
        self.assertFalse(upload_stat.try_start("-100123", 42, file_name="a.mp4"))

        upload_stat.remove("-100123", 42)
        self.assertFalse(upload_stat.is_uploading(-100123, 42))
        self.assertTrue(upload_stat.try_start(-100123, 42, file_name="a.mp4"))


if __name__ == "__main__":
    unittest.main()
