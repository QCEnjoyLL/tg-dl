"""Tests for the resumable sequential download path."""

import os
import tempfile
import unittest

from module import pyrogram_guard
from module.pyrogram_guard import CHUNK_SIZE


class ResumeOffsetTestCase(unittest.TestCase):
    def test_partial_file_resumes_at_the_last_complete_chunk(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_path = os.path.join(tmp_dir, "video.bin.temp")
            with open(temp_path, "wb") as file_obj:
                file_obj.write(b"x" * (2 * CHUNK_SIZE + 5))

            offset = pyrogram_guard._resume_offset(  # pylint: disable = W0212
                temp_path, 10 * CHUNK_SIZE
            )

            # The partial 3rd chunk is dropped so appending cannot duplicate it.
            self.assertEqual(offset, 2 * CHUNK_SIZE)
            self.assertEqual(os.path.getsize(temp_path), 2 * CHUNK_SIZE)

    def test_complete_leftover_is_treated_as_finished(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_path = os.path.join(tmp_dir, "video.bin.temp")
            with open(temp_path, "wb") as file_obj:
                file_obj.write(b"x" * CHUNK_SIZE)

            self.assertEqual(
                pyrogram_guard._resume_offset(  # pylint: disable = W0212
                    temp_path, CHUNK_SIZE
                ),
                CHUNK_SIZE,
            )

    def test_missing_or_empty_file_starts_at_zero(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing = os.path.join(tmp_dir, "missing.temp")
            self.assertEqual(
                pyrogram_guard._resume_offset(  # pylint: disable = W0212
                    missing, CHUNK_SIZE
                ),
                0,
            )

            empty = os.path.join(tmp_dir, "empty.temp")
            open(empty, "wb").close()
            self.assertEqual(
                pyrogram_guard._resume_offset(  # pylint: disable = W0212
                    empty, CHUNK_SIZE
                ),
                0,
            )


if __name__ == "__main__":
    unittest.main()
