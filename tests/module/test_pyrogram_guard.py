"""Regression tests for the resume-on-truncation get_file guard."""

import asyncio
import logging
import unittest
from unittest import mock

import pyrogram

from module import pyrogram_guard
from module.pyrogram_guard import (
    CHUNK_SIZE,
    DownloadIncomplete,
    _captured_error,
    guard_get_file,
    guard_handle_download,
)


class FakeStream:
    """Stands in for pyrogram's get_file, including its silent truncation.

    ``fail_offsets`` lists byte offsets where a ``GetFile`` request "fails":
    the generator simply stops there, exactly like the fork does after logging
    the exception it swallowed.
    """

    def __init__(self, data: bytes, fail_offsets=None):
        self.data = data
        self.fail_offsets = list(fail_offsets or [])
        self.calls = []

    async def stream(
        self,
        client,
        file_id,
        file_size=0,
        limit=0,
        offset=0,
        progress=None,
        progress_args=(),
    ):
        self.calls.append(offset)
        start = offset * CHUNK_SIZE
        position = start
        while position < len(self.data):
            for fail_at in list(self.fail_offsets):
                if fail_at <= position:
                    self.fail_offsets.remove(fail_at)
                    return
            chunk = self.data[position : position + CHUNK_SIZE]
            position += len(chunk)
            yield chunk


class StalledStream:
    """A get_file whose requests always fail: it yields nothing at all."""

    async def stream(self, *args, **kwargs):
        for _ in ():
            yield b""


class FailingStream:
    """A get_file that raises a protocol error instead of ending silently."""

    def __init__(self, error: BaseException):
        self.error = error
        self.calls = 0

    async def stream(self, *args, **kwargs):
        self.calls += 1
        raise self.error
        yield b""  # pragma: no cover


class FloodingStream:
    """Raises a flood error a few times, then streams the data normally."""

    def __init__(self, data: bytes, error: BaseException, failures: int = 1):
        self.data = data
        self.error = error
        self.failures = failures
        self.calls = 0

    async def stream(
        self,
        client,
        file_id,
        file_size=0,
        limit=0,
        offset=0,
        progress=None,
        progress_args=(),
    ):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        start = offset * CHUNK_SIZE
        position = start
        while position < len(self.data):
            chunk = self.data[position : position + CHUNK_SIZE]
            position += len(chunk)
            yield chunk


class GuardGetFileTestCase(unittest.IsolatedAsyncioTestCase):
    async def collect(self, stream, file_size):
        guarded = guard_get_file(stream.stream)
        return b"".join([chunk async for chunk in guarded(None, "file", file_size)])

    async def test_flood_wait_is_waited_out_and_the_stream_resumes(self):
        # FLOOD_PREMIUM_WAIT is Telegram's free-account throttle; aborting the
        # stream made download_media restart a multi-GB file from zero.
        error = pyrogram.errors.exceptions.flood_420.FloodPremiumWait(value=0)
        data = b"f" * (2 * CHUNK_SIZE)
        stream = FloodingStream(data, error)

        with (
            mock.patch.object(pyrogram_guard, "RESUME_SLEEP", 0),
            mock.patch.object(pyrogram_guard, "FLOOD_WAIT_MARGIN", 0),
        ):
            result = await self.collect(stream, len(data))

        self.assertEqual(result, data)
        self.assertEqual(stream.calls, 2)

    async def test_endless_floods_are_reported_as_incomplete(self):
        error = pyrogram.errors.exceptions.flood_420.FloodPremiumWait(value=0)
        stream = FloodingStream(b"g" * CHUNK_SIZE, error, failures=99)

        with (
            mock.patch.object(pyrogram_guard, "RESUME_SLEEP", 0),
            mock.patch.object(pyrogram_guard, "FLOOD_WAIT_MARGIN", 0),
            mock.patch.object(pyrogram_guard, "MAX_FLOOD_WAITS_PER_STREAM", 2),
        ):
            with self.assertRaises(DownloadIncomplete) as caught:
                await self.collect(stream, CHUNK_SIZE)

        self.assertEqual(caught.exception.received, 0)
        self.assertIsInstance(caught.exception.cause, type(error))

    async def test_complete_stream_is_passed_through(self):
        data = b"x" * (CHUNK_SIZE + 123)
        stream = FakeStream(data)

        result = await self.collect(stream, len(data))

        self.assertEqual(result, data)
        self.assertEqual(stream.calls, [0])

    async def test_truncated_stream_resumes_and_finishes(self):
        data = b"y" * (2 * CHUNK_SIZE + 500)
        # The first GetFile stream dies after the first chunk, like a dropped
        # request in the fork. The guard must reopen it at chunk 1.
        stream = FakeStream(data, fail_offsets=[CHUNK_SIZE])

        with mock.patch.object(pyrogram_guard, "RESUME_SLEEP", 0):
            result = await self.collect(stream, len(data))

        self.assertEqual(result, data)
        self.assertEqual(stream.calls, [0, 1])

    async def test_repeated_truncation_keeps_resuming(self):
        data = b"z" * (4 * CHUNK_SIZE)
        stream = FakeStream(
            data,
            fail_offsets=[CHUNK_SIZE, 2 * CHUNK_SIZE, 3 * CHUNK_SIZE],
        )

        with mock.patch.object(pyrogram_guard, "RESUME_SLEEP", 0):
            result = await self.collect(stream, len(data))

        self.assertEqual(result, data)
        self.assertEqual(stream.calls, [0, 1, 2, 3])

    async def test_stalled_stream_raises_download_incomplete(self):
        data = b"q" * (2 * CHUNK_SIZE)

        with mock.patch.object(pyrogram_guard, "RESUME_SLEEP", 0):
            with self.assertRaises(DownloadIncomplete) as caught:
                await self.collect(StalledStream(), len(data))

        self.assertEqual(caught.exception.received, 0)
        self.assertEqual(caught.exception.expected, len(data))

    async def test_server_timeout_fails_fast_with_cause(self):
        """[-500 TIMEOUT] means the storage node is not serving the file.

        Reopening the stream only waits for another timeout, so the guard must
        surface the cause on the first attempt.
        """
        timeout = pyrogram.errors.exceptions.internal_server_error_500.Timeout()
        stream = FailingStream(timeout)

        with self.assertRaises(DownloadIncomplete) as caught:
            await self.collect(stream, 2 * CHUNK_SIZE)

        self.assertIs(caught.exception.cause, timeout)
        self.assertIn("Timeout", str(caught.exception))
        self.assertEqual(stream.calls, 1)

    async def test_unexpected_error_is_attached_to_download_incomplete(self):
        error = RuntimeError("connection reset by peer")
        stream = FailingStream(error)

        with self.assertRaises(DownloadIncomplete) as caught:
            await self.collect(stream, CHUNK_SIZE)

        self.assertIs(caught.exception.cause, error)

    async def test_cancelled_error_is_not_swallowed(self):
        stream = FailingStream(asyncio.CancelledError())

        with self.assertRaises(asyncio.CancelledError):
            await self.collect(stream, CHUNK_SIZE)

    async def test_swallowed_exception_is_captured_from_pyrogram_log(self):
        """The fork logs the error it swallowed; the guard must report it."""
        timeout = pyrogram.errors.exceptions.internal_server_error_500.Timeout()
        pyrogram_guard._install_error_capture()  # pylint: disable = W0212

        async def stream(*args, **kwargs):
            logging.getLogger("pyrogram.client").error(
                "get_file failed", exc_info=timeout
            )
            for _ in ():
                yield b""

        holder = mock.Mock(stream=stream)
        with mock.patch.object(pyrogram_guard, "RESUME_SLEEP", 0):
            with self.assertRaises(DownloadIncomplete) as caught:
                await self.collect(holder, CHUNK_SIZE)

        self.assertIs(caught.exception.cause, timeout)

    def test_handle_download_reraises_download_incomplete(self):
        """Stock Pyrogram returns None; the guard must propagate the failure."""

        async def original_handle_download(_self, _packet):
            _captured_error.set(DownloadIncomplete(10, 100))
            return None

        guarded = guard_handle_download(original_handle_download)

        async def run():
            return await guarded(None, None)

        with self.assertRaises(DownloadIncomplete) as caught:
            asyncio.run(run())

        self.assertEqual(caught.exception.received, 10)

    def test_handle_download_passes_success_through(self):
        async def original_handle_download(_self, _packet):
            return "/downloads/file.mp4"

        guarded = guard_handle_download(original_handle_download)

        async def run():
            return await guarded(None, None)

        self.assertEqual(asyncio.run(run()), "/downloads/file.mp4")

    async def test_unknown_size_stream_is_not_guarded(self):
        data = b"w" * 100
        stream = FakeStream(data)

        guarded = guard_get_file(stream.stream)
        result = b"".join([chunk async for chunk in guarded(None, "file", 0)])

        self.assertEqual(result, data)
        self.assertEqual(stream.calls, [0])

    def test_install_is_idempotent(self):
        # media_downloader.main() installs the guard, and under some pytest
        # versions the media_downloader tests run before this module, so the
        # first call here may already be a no-op. Idempotency is the contract.
        original_get_file = pyrogram.Client.get_file
        original_handle_download = pyrogram.Client.handle_download
        try:
            pyrogram_guard.install_get_file_guard()
            self.assertFalse(pyrogram_guard.install_get_file_guard())
            self.assertTrue(getattr(pyrogram.Client.get_file, "__tdl_guarded__", False))
            self.assertTrue(
                getattr(pyrogram.Client.handle_download, "__tdl_guarded__", False)
            )
        finally:
            pyrogram.Client.get_file = original_get_file
            pyrogram.Client.handle_download = original_handle_download
