"""Regression tests for the resume-on-truncation get_file guard."""

import unittest
from unittest import mock

import pyrogram

from module import pyrogram_guard
from module.pyrogram_guard import (
    CHUNK_SIZE,
    DownloadIncomplete,
    guard_get_file,
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

    async def stream(self, client, file_id, file_size=0, limit=0, offset=0,
                     progress=None, progress_args=()):
        self.calls.append(offset)
        start = offset * CHUNK_SIZE
        position = start
        while position < len(self.data):
            for fail_at in list(self.fail_offsets):
                if fail_at <= position:
                    self.fail_offsets.remove(fail_at)
                    return
            chunk = self.data[position:position + CHUNK_SIZE]
            position += len(chunk)
            yield chunk


class StalledStream:
    """A get_file whose requests always fail: it yields nothing at all."""

    async def stream(self, *args, **kwargs):
        for _ in ():
            yield b""


class GuardGetFileTestCase(unittest.IsolatedAsyncioTestCase):
    async def collect(self, stream, file_size):
        guarded = guard_get_file(stream.stream)
        return b"".join([chunk async for chunk in guarded(None, "file", file_size)])

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

    async def test_unknown_size_stream_is_not_guarded(self):
        data = b"w" * 100
        stream = FakeStream(data)

        guarded = guard_get_file(stream.stream)
        result = b"".join([chunk async for chunk in guarded(None, "file", 0)])

        self.assertEqual(result, data)
        self.assertEqual(stream.calls, [0])

    def test_install_is_idempotent(self):
        original = pyrogram.Client.get_file
        try:
            self.assertTrue(pyrogram_guard.install_get_file_guard())
            self.assertFalse(pyrogram_guard.install_get_file_guard())
            self.assertTrue(
                getattr(pyrogram.Client.get_file, "__tdl_guarded__", False)
            )
        finally:
            pyrogram.Client.get_file = original
