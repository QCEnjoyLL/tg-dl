"""Tests for the resumable sequential download path."""

import json
import os
import tempfile
import unittest

from module import fast_download, pyrogram_guard
from module.pyrogram_guard import CHUNK_SIZE, PARALLEL_STATE_SUFFIX


class _SequentialClient:
    def __init__(self, data: bytes):
        self.data = data
        self.offsets = []

    async def get_file(
        self,
        _file_id,
        _file_size=0,
        _limit=0,
        offset=0,
        _progress=None,
        _progress_args=(),
    ):
        self.offsets.append(offset)
        pos = offset * CHUNK_SIZE
        while pos < len(self.data):
            yield self.data[pos : pos + CHUNK_SIZE]
            pos += CHUNK_SIZE


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

    def test_parallel_sidecar_discards_sparse_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_path = os.path.join(tmp_dir, "video.bin.temp")
            sidecar_path = temp_path + PARALLEL_STATE_SUFFIX
            file_size = 4 * CHUNK_SIZE

            # Simulate parallel tail range finished first so file size == file_size
            # while the beginning of the file is still zero-filled holes.
            with open(temp_path, "wb") as file_obj:
                file_obj.seek(3 * CHUNK_SIZE)
                file_obj.write(b"t" * CHUNK_SIZE)
            with open(sidecar_path, "w", encoding="utf-8") as sidecar_file:
                json.dump([0, 0, 0, CHUNK_SIZE], sidecar_file)

            offset = pyrogram_guard._resume_offset(  # pylint: disable = W0212
                temp_path, file_size
            )

            self.assertEqual(offset, 0)
            self.assertFalse(os.path.exists(temp_path))
            self.assertFalse(os.path.exists(sidecar_path))

    def test_parallel_sidecar_discards_partial_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_path = os.path.join(tmp_dir, "video.bin.temp")
            sidecar_path = temp_path + PARALLEL_STATE_SUFFIX
            with open(temp_path, "wb") as file_obj:
                file_obj.write(b"a" * CHUNK_SIZE)
            with open(sidecar_path, "w", encoding="utf-8") as sidecar_file:
                json.dump([CHUNK_SIZE, 0], sidecar_file)

            offset = pyrogram_guard._resume_offset(  # pylint: disable = W0212
                temp_path, 2 * CHUNK_SIZE
            )

            self.assertEqual(offset, 0)
            self.assertFalse(os.path.exists(temp_path))
            self.assertFalse(os.path.exists(sidecar_path))


class ParallelToSequentialFallbackTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_switching_to_sequential_after_parallel_sparse_file_restarts_cleanly(self):
        saved = fast_download.get_chunk_concurrency()
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                data = os.urandom(3 * CHUNK_SIZE)
                temp_path = os.path.join(tmp_dir, "video.bin.temp")
                sidecar_path = temp_path + PARALLEL_STATE_SUFFIX

                # Tail range was written by a prior parallel attempt (size == len(data),
                # leading ranges still zero holes).
                with open(temp_path, "wb") as file_obj:
                    file_obj.seek(2 * CHUNK_SIZE)
                    file_obj.write(data[2 * CHUNK_SIZE :])
                with open(sidecar_path, "w", encoding="utf-8") as sidecar_file:
                    json.dump([0, 0, CHUNK_SIZE], sidecar_file)

                fast_download.set_chunk_concurrency(1)
                client = _SequentialClient(data)

                async def fail_original(*_args):
                    raise AssertionError("stock handle_download should not be called")

                handle = fast_download.parallel_handle_download(fail_original)
                path = await handle(
                    client,
                    ("file-id", tmp_dir, "video.bin", False, len(data), None, ()),
                )

                self.assertEqual(path, os.path.join(tmp_dir, "video.bin"))
                self.assertEqual(client.offsets, [0])
                with open(path, "rb") as file_obj:
                    self.assertEqual(file_obj.read(), data)
                self.assertFalse(os.path.exists(sidecar_path))
        finally:
            fast_download.set_chunk_concurrency(saved)


if __name__ == "__main__":
    unittest.main()
