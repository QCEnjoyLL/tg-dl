"""Opt-in parallel chunk download for a single file.

``upload.GetFile`` returns at most 1 MiB per request, and the bundled Pyrogram
fetches those chunks strictly one after another. On any link with latency that
caps one file at roughly ``1 MiB / RTT`` no matter how fast the line is —
``max_download_task`` only helps when *several* files are queued.

``max_concurrent_chunks`` splits one file into N contiguous chunk ranges and
fetches them concurrently over the same media session. MTProto multiplexes the
requests, so this opens no extra connection, session or auth key; it just keeps
N requests in flight instead of one. Every range keeps the resume semantics of
the sequential path: a truncated range is reopened at the last complete chunk
boundary, and repeated stalls raise ``DownloadIncomplete`` so ``download_media``
can retry with backoff.

The default is 1, which leaves the stock sequential path untouched.
"""

import asyncio
import math
import os
import re
import shutil
from typing import List, Optional, Tuple

from loguru import logger

from module import pyrogram_guard

#: One range == one in-flight GetFile request; going past this mostly buys
#: FloodWait instead of bandwidth.
MAX_CHUNK_CONCURRENCY = 16

_concurrency = 1


def set_chunk_concurrency(value) -> int:
    """Set how many ranges one file is split into (1 = sequential download)."""
    # pylint: disable = W0603
    global _concurrency
    try:
        chunks = int(value)
    except (TypeError, ValueError):
        logger.warning(f"max_concurrent_chunks 不是整数（{value!r}），按 1 处理")
        chunks = 1
    if chunks < 1:
        logger.warning(f"max_concurrent_chunks={chunks} 非法，按 1 处理")
        chunks = 1
    if chunks > MAX_CHUNK_CONCURRENCY:
        logger.warning(
            f"max_concurrent_chunks={chunks} 过大，已限制为 {MAX_CHUNK_CONCURRENCY}"
        )
        chunks = MAX_CHUNK_CONCURRENCY
    _concurrency = chunks
    return _concurrency


def get_chunk_concurrency() -> int:
    """How many ranges one file is split into right now."""
    return _concurrency


def describe_chunk_settings() -> Optional[str]:
    """One-line summary for the startup log, or ``None`` when disabled."""
    if _concurrency <= 1:
        return None
    return f"单文件并行下载：{_concurrency} 路"


def chunk_ranges(file_size: int, concurrency: int) -> List[Tuple[int, int]]:
    """Split ``file_size`` into contiguous ``(start, end)`` chunk indexes.

    Ends are exclusive. Small files simply get fewer ranges, so the caller never
    has to special-case a size below ``concurrency * CHUNK_SIZE``.
    """
    if file_size <= 0:
        return []
    total_chunks = math.ceil(file_size / pyrogram_guard.CHUNK_SIZE)
    parts = max(1, min(concurrency, total_chunks))
    per_part = math.ceil(total_chunks / parts)
    ranges = []
    for index in range(parts):
        start = index * per_part
        if start >= total_chunks:
            break
        ranges.append((start, min(start + per_part, total_chunks)))
    return ranges


class _ChunkWriter:
    """Write chunks at absolute offsets and publish a monotonic byte total."""

    def __init__(self, file_obj, file_size: int, progress, progress_args):
        self._file = file_obj
        self._size = file_size
        self._progress = progress
        self._progress_args = progress_args
        self._write_lock = asyncio.Lock()
        # The progress callback derives the speed from deltas, so it must never
        # see a smaller total after a bigger one.
        self._progress_lock = asyncio.Lock()
        self.received = 0

    async def write(self, offset: int, chunk: bytes) -> None:
        """Store ``chunk`` at ``offset`` and report the aggregated progress."""
        async with self._write_lock:
            self._file.seek(offset)
            self._file.write(chunk)
            self.received += len(chunk)
            received = self.received

        if self._progress is None:
            return
        async with self._progress_lock:
            await self._progress(received, self._size, *self._progress_args)


async def _download_range(
    client,
    file_id,
    file_size: int,
    start_chunk: int,
    end_chunk: int,
    writer: _ChunkWriter,
    label: str,
) -> None:
    """Fetch chunks ``[start_chunk, end_chunk)`` of one file."""
    chunk_size = pyrogram_guard.CHUNK_SIZE
    range_start = start_chunk * chunk_size
    expected = min(end_chunk * chunk_size, file_size) - range_start
    if expected <= 0:
        return

    received = 0
    stalled = 0
    while received < expected:
        absolute = range_start + received
        chunk_index = absolute // chunk_size
        # A previous attempt may have stopped mid-chunk, so the first chunk of a
        # resumed stream can carry a few bytes that are already on disk.
        skip = absolute - chunk_index * chunk_size
        progressed = False

        async for chunk in client.get_file(
            file_id, file_size, end_chunk - chunk_index, chunk_index
        ):
            if skip:
                if len(chunk) <= skip:
                    skip -= len(chunk)
                    continue
                chunk = chunk[skip:]
                skip = 0
            if not chunk:
                continue
            if received + len(chunk) > expected:
                # A stream can end with a short chunk; never write into the
                # next range.
                chunk = chunk[: expected - received]

            await writer.write(absolute, chunk)
            absolute += len(chunk)
            received += len(chunk)
            progressed = True
            if received >= expected:
                break

        if received >= expected:
            break

        if progressed:
            stalled = 0
        else:
            stalled += 1
            if stalled >= pyrogram_guard.MAX_STALLED_ATTEMPTS:
                raise pyrogram_guard.DownloadIncomplete(received, expected)
        logger.debug(
            f"{label}: range {start_chunk}-{end_chunk} resumed after "
            f"{received}/{expected} bytes"
        )
        await asyncio.sleep(pyrogram_guard.RESUME_SLEEP)


async def _run_ranges(client, file_id, file_size: int, writer: _ChunkWriter, label):
    tasks = [
        asyncio.ensure_future(
            _download_range(client, file_id, file_size, start, end, writer, label)
        )
        for start, end in chunk_ranges(file_size, _concurrency)
    ]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def parallel_handle_download(original):
    """Wrap ``Client.handle_download``; defer to ``original`` when disabled.

    The wrapper reproduces the stock contract exactly — on success it returns the
    final path, on failure it removes the ``.temp`` file and returns ``None``
    while ``FloodWait`` / cancellation propagate — so
    ``pyrogram_guard.guard_handle_download`` keeps turning a swallowed
    ``DownloadIncomplete`` into an exception.
    """

    async def handle_download(self, packet):
        (
            file_id,
            directory,
            file_name,
            in_memory,
            file_size,
            progress,
            progress_args,
        ) = packet
        # In-memory (BytesIO) downloads and unknown sizes keep the stock path.
        if _concurrency <= 1 or in_memory or not file_size:
            return await original(self, packet)

        os.makedirs(directory, exist_ok=True)
        temp_file_path = (
            os.path.abspath(re.sub("\\\\", "/", os.path.join(directory, file_name)))
            + ".temp"
        )
        label = f"[{file_name}]"

        try:
            with open(temp_file_path, "wb") as file_obj:
                writer = _ChunkWriter(file_obj, file_size, progress, progress_args)
                await _run_ranges(self, file_id, file_size, writer, label)
                received = writer.received
        except BaseException as error:  # pylint: disable = W0703
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
            if isinstance(
                error, (asyncio.CancelledError,) + pyrogram_guard.propagated_errors()
            ):
                raise
            # The stock implementation hides the cause; keep it in the context
            # variable so the guard can report it and let download_media retry.
            pyrogram_guard.record_swallowed_error(error)
            logger.warning(f"{label} parallel download failed: {error}")
            return None

        if received != file_size:
            logger.warning(
                f"{label} parallel download incomplete: {received}/{file_size}"
            )
            pyrogram_guard.record_swallowed_error(
                pyrogram_guard.DownloadIncomplete(received, file_size)
            )
            return None

        final_path = os.path.splitext(temp_file_path)[0]
        shutil.move(temp_file_path, final_path)
        return final_path

    handle_download.__tdl_parallel__ = True
    return handle_download
