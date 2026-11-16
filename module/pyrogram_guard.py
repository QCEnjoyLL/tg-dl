"""Work around the bundled Pyrogram fork's silent truncated downloads.

``tangyoha/pyrogram``'s ``Client.get_file`` catches every non-FloodWait
exception and only logs it, so a dropped ``upload.GetFile`` request ends the
chunk stream quietly. ``handle_download`` then moves the partial ``.temp`` file
into place and returns it as if the download had succeeded -- the caller only
notices through a size check. That is why a stalled 210 MB transfer appeared as
"downloaded with wrong size: 5242880" and was then reported as an expired file
reference.

The wrapper below keeps the stream honest: when the generator ends before
``file_size`` bytes were produced, it reopens the stream at the last chunk
boundary with a ``GetFile`` offset and keeps appending. Only after repeated
zero-progress attempts does it raise ``DownloadIncomplete``, so the downloader
can retry or fail with a clear reason instead of silently accepting a partial
file.
"""

import asyncio
import functools

#: Telegram's ``upload.GetFile`` returns at most 1 MiB per request.
CHUNK_SIZE = 1024 * 1024

#: Consecutive attempts that made no progress at all before giving up.
MAX_STALLED_ATTEMPTS = 3

#: Pause between resume attempts (tests override this).
RESUME_SLEEP = 1.0


class DownloadIncomplete(Exception):
    """The file stream ended before the announced size was received."""

    def __init__(self, received: int, expected: int):
        super().__init__(f"only received {received} of {expected} bytes")
        self.received = received
        self.expected = expected


def guard_get_file(original):
    """Wrap ``Client.get_file`` with resume-on-truncation semantics."""

    @functools.wraps(original)
    async def guarded_get_file(
        self,
        file_id,
        file_size: int = 0,
        limit: int = 0,
        offset: int = 0,
        progress=None,
        progress_args=(),
    ):
        # Only the plain "download the whole file" call is guarded. Callers that
        # ask for a limited range keep the fork's behaviour.
        if not file_size or limit or offset:
            async for chunk in original(
                self, file_id, file_size, limit, offset, progress, progress_args
            ):
                yield chunk
            return

        received = 0
        stalled = 0
        while received < file_size:
            start_chunk = received // CHUNK_SIZE
            skip = received - start_chunk * CHUNK_SIZE
            progressed = False

            async for chunk in original(
                self, file_id, file_size, 0, start_chunk, progress, progress_args
            ):
                if skip:
                    if len(chunk) <= skip:
                        skip -= len(chunk)
                        continue
                    chunk = chunk[skip:]
                    skip = 0
                if not chunk:
                    continue
                received += len(chunk)
                progressed = True
                yield chunk
                if received >= file_size:
                    break

            if received >= file_size:
                break
            if progressed:
                stalled = 0
            else:
                stalled += 1
                if stalled >= MAX_STALLED_ATTEMPTS:
                    raise DownloadIncomplete(received, file_size)
            await asyncio.sleep(RESUME_SLEEP)

    guarded_get_file.__tdl_guarded__ = True
    return guarded_get_file


def install_get_file_guard() -> bool:
    """Install the guard once per process. Returns whether it was installed."""
    import pyrogram

    original = pyrogram.Client.get_file
    if getattr(original, "__tdl_guarded__", False):
        return False
    pyrogram.Client.get_file = guard_get_file(original)
    return True
