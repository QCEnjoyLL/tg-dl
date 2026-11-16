"""Tests for the opt-in per-file parallel chunk downloader."""

import os
import tempfile
import unittest
from unittest import mock

import pyrogram

from module import pyrogram_guard
from module.fast_download import (
    chunk_ranges,
    get_chunk_concurrency,
    parallel_handle_download,
    set_chunk_concurrency,
)
from module.pyrogram_guard import CHUNK_SIZE, DownloadIncomplete, _captured_error


class FakeGetFileClient:
    """Serves ``data`` in 1 MiB chunks, like the bundled fork's ``get_file``.

    With ``truncate_first`` the first request of every ``(offset, limit)`` pair
    returns a short chunk and then ends the stream, which is the "silently
    stopped" failure the guard exists for; the retry serves the range fully.
    """

    def __init__(self, data: bytes, truncate_first: bool = False):
        self.data = data
        self.truncate_first = truncate_first
        self.calls = {}

    @property
    def offsets(self):
        return [offset for offset, _limit in self.calls]

    def _stop(self, start: int, limit: int) -> int:
        return (
            len(self.data)
            if not limit
            else min(start + limit * CHUNK_SIZE, len(self.data))
        )

    async def get_file(self, file_id, file_size=0, limit=0, offset=0):
        key = (offset, limit)
        first_call = key not in self.calls
        self.calls[key] = self.calls.get(key, 0) + 1
        start = offset * CHUNK_SIZE
        stop = self._stop(start, limit)

        if self.truncate_first and first_call:
            yield self.data[start : min(start + 10, stop)]
            return

        position = start
        while position < stop:
            chunk = self.data[position : min(position + CHUNK_SIZE, stop)]
            position += len(chunk)
            yield chunk


class SilentGetFileClient:
    """A ``get_file`` whose requests always come back empty."""

    async def get_file(self, *_args, **_kwargs):
        for _ in ():
            yield b""


class FailingGetFileClient:
    """A ``get_file`` that raises instead of ending the stream."""

    def __init__(self, error: BaseException):
        self.error = error

    async def get_file(self, *_args, **_kwargs):
        raise self.error
        yield b""  # pragma: no cover


class FloodThenStream:
    """Raises ``error`` on the first call, then serves ``data``."""

    def __init__(self, error: BaseException, data: bytes):
        self.error = error
        self.data = data
        self.calls = 0

    async def get_file(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            raise self.error
        yield self.data


class AlwaysFlooding:
    """A ``get_file`` that only ever raises the given flood error."""

    def __init__(self, error: BaseException):
        self.error = error

    async def get_file(self, *_args, **_kwargs):
        raise self.error
        yield b""  # pragma: no cover


class ChunkRangesTestCase(unittest.TestCase):
    def test_splits_into_requested_ranges(self):
        self.assertEqual(
            chunk_ranges(4 * CHUNK_SIZE, 4),
            [(0, 1), (1, 2), (2, 3), (3, 4)],
        )

    def test_small_file_uses_a_single_range(self):
        self.assertEqual(chunk_ranges(100, 8), [(0, 1)])

    def test_partial_last_chunk_still_counts(self):
        self.assertEqual(chunk_ranges(CHUNK_SIZE + 10, 4), [(0, 1), (1, 2)])

    def test_empty_file_has_no_ranges(self):
        self.assertEqual(chunk_ranges(0, 4), [])


class ChunkConcurrencyTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = get_chunk_concurrency()

    def tearDown(self):
        set_chunk_concurrency(self._saved)

    def test_values_are_clamped(self):
        self.assertEqual(set_chunk_concurrency(4), 4)
        self.assertEqual(set_chunk_concurrency(99), 16)
        self.assertEqual(set_chunk_concurrency(0), 1)
        self.assertEqual(set_chunk_concurrency(-3), 1)
        self.assertEqual(set_chunk_concurrency("nope"), 1)
        self.assertEqual(set_chunk_concurrency("6"), 6)


class ParallelDownloadTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = self._tmp.name
        self._saved = get_chunk_concurrency()
        set_chunk_concurrency(4)

    def tearDown(self):
        set_chunk_concurrency(self._saved)
        self._tmp.cleanup()

    def packet(self, file_size, file_name="video.bin", in_memory=False):
        return ("file-id", self.directory, file_name, in_memory, file_size, None, ())

    async def fail_original(self, *_args):
        raise AssertionError("the stock handle_download must not be called")

    async def test_reassembles_all_ranges_into_one_file(self):
        data = os.urandom(3 * CHUNK_SIZE + 123)
        client = FakeGetFileClient(data)
        handle = parallel_handle_download(self.fail_original)

        path = await handle(client, self.packet(len(data)))

        self.assertEqual(path, os.path.join(self.directory, "video.bin"))
        with open(path, "rb") as file:
            self.assertEqual(file.read(), data)
        self.assertFalse(os.path.exists(path + ".temp"))
        # Four chunks, four ranges, one request each.
        self.assertEqual(len(client.calls), 4)

    async def test_progress_is_monotonic_and_reaches_file_size(self):
        data = os.urandom(2 * CHUNK_SIZE)
        client = FakeGetFileClient(data)
        seen = []

        async def progress(current, total):
            seen.append((current, total))

        handle = parallel_handle_download(self.fail_original)
        packet = (
            "file-id",
            self.directory,
            "video.bin",
            False,
            len(data),
            progress,
            (),
        )
        await handle(client, packet)

        self.assertEqual(
            [current for current, _ in seen], sorted(current for current, _ in seen)
        )
        self.assertEqual(seen[-1], (len(data), len(data)))

    async def test_truncated_range_resumes_and_completes(self):
        data = os.urandom(2 * CHUNK_SIZE)
        client = FakeGetFileClient(data, truncate_first=True)
        handle = parallel_handle_download(self.fail_original)

        with mock.patch.object(pyrogram_guard, "RESUME_SLEEP", 0):
            path = await handle(client, self.packet(len(data)))

        with open(path, "rb") as file:
            self.assertEqual(file.read(), data)
        self.assertTrue(
            any(count > 1 for count in client.calls.values()),
            "a truncated range should have been reopened",
        )

    async def test_repeated_stalls_return_none_and_clean_up(self):
        handle = parallel_handle_download(self.fail_original)
        _captured_error.set(None)

        with mock.patch.object(pyrogram_guard, "RESUME_SLEEP", 0):
            result = await handle(SilentGetFileClient(), self.packet(CHUNK_SIZE))

        self.assertIsNone(result)
        self.assertFalse(
            any(name.endswith(".temp") for name in os.listdir(self.directory))
        )
        self.assertIsInstance(_captured_error.get(), DownloadIncomplete)

    async def test_short_write_is_reported_as_incomplete(self):
        """A truncated last chunk must not be accepted as a complete file."""

        class ShortClient:
            async def get_file(self, *_args, **_kwargs):
                yield b"x" * 10

        handle = parallel_handle_download(self.fail_original)
        _captured_error.set(None)

        with mock.patch.object(pyrogram_guard, "RESUME_SLEEP", 0):
            result = await handle(ShortClient(), self.packet(CHUNK_SIZE))

        self.assertIsNone(result)
        self.assertIsInstance(_captured_error.get(), DownloadIncomplete)

    async def test_disabled_uses_the_stock_implementation(self):
        set_chunk_concurrency(1)
        called = []

        async def original(_self, packet):
            called.append(packet)
            return "/stock/path"

        handle = parallel_handle_download(original)
        result = await handle(None, self.packet(CHUNK_SIZE))

        self.assertEqual(result, "/stock/path")
        self.assertEqual(len(called), 1)

    async def test_in_memory_download_uses_the_stock_implementation(self):
        called = []

        async def original(_self, packet):
            called.append(packet)
            return None

        handle = parallel_handle_download(original)
        await handle(None, self.packet(CHUNK_SIZE, in_memory=True))

        self.assertEqual(len(called), 1)

    async def test_protocol_error_fails_the_download(self):
        error = RuntimeError("connection reset by peer")
        handle = parallel_handle_download(self.fail_original)
        _captured_error.set(None)

        result = await handle(FailingGetFileClient(error), self.packet(CHUNK_SIZE))

        self.assertIsNone(result)
        self.assertIs(_captured_error.get(), error)

    async def test_flood_wait_resumes_the_range(self):
        # FLOOD_PREMIUM_WAIT is Telegram throttling the account; waiting keeps
        # the bytes already written instead of failing the file.
        error = pyrogram.errors.exceptions.flood_420.FloodPremiumWait(value=0)
        data = os.urandom(CHUNK_SIZE)
        client = FloodThenStream(error, data)
        handle = parallel_handle_download(self.fail_original)

        with mock.patch.object(pyrogram_guard, "FLOOD_WAIT_MARGIN", 0):
            path = await handle(client, self.packet(len(data)))

        with open(path, "rb") as file:
            self.assertEqual(file.read(), data)
        self.assertEqual(client.calls, 2)

    async def test_endless_floods_are_reported_incomplete(self):
        error = pyrogram.errors.exceptions.flood_420.FloodPremiumWait(value=0)
        handle = parallel_handle_download(self.fail_original)
        _captured_error.set(None)

        with mock.patch.object(
            pyrogram_guard, "FLOOD_WAIT_MARGIN", 0
        ), mock.patch.object(pyrogram_guard, "MAX_FLOOD_WAITS_PER_STREAM", 2):
            result = await handle(AlwaysFlooding(error), self.packet(CHUNK_SIZE))

        self.assertIsNone(result)
        self.assertIsInstance(_captured_error.get(), DownloadIncomplete)


class InstalledGuardTestCase(unittest.IsolatedAsyncioTestCase):
    """The patch chain installed by ``media_downloader.main`` must reach us."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = self._tmp.name
        self._saved_concurrency = get_chunk_concurrency()
        self._saved_get_file = pyrogram.Client.get_file
        self._saved_handle_download = pyrogram.Client.handle_download
        self.stock_calls = []

        async def stock_handle_download(_self, packet):
            self.stock_calls.append(packet)
            return "/stock/path"

        pyrogram.Client.handle_download = stock_handle_download
        pyrogram_guard.install_get_file_guard()

    def tearDown(self):
        set_chunk_concurrency(self._saved_concurrency)
        pyrogram.Client.get_file = self._saved_get_file
        pyrogram.Client.handle_download = self._saved_handle_download
        self._tmp.cleanup()

    async def test_installed_chain_uses_the_parallel_downloader(self):
        set_chunk_concurrency(4)
        data = os.urandom(2 * CHUNK_SIZE)
        packet = ("file-id", self.directory, "video.bin", False, len(data), None, ())

        path = await pyrogram.Client.handle_download(FakeGetFileClient(data), packet)

        self.assertEqual(path, os.path.join(self.directory, "video.bin"))
        self.assertEqual(self.stock_calls, [])

    async def test_installed_chain_falls_back_when_disabled(self):
        set_chunk_concurrency(1)
        packet = ("file-id", self.directory, "video.bin", False, CHUNK_SIZE, None, ())

        self.assertEqual(
            await pyrogram.Client.handle_download(None, packet), "/stock/path"
        )


if __name__ == "__main__":
    unittest.main()
