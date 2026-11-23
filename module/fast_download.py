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

A failed attempt now keeps both the partial ``.temp`` file and a small
``.temp.json`` sidecar recording how many bytes of every range are already on
disk, so the next attempt (or a restart) continues instead of downloading a
multi-GB file from zero again. Ranges write out of order, which is why the
sidecar is required: the file length alone cannot tell which ranges are done.

The default is 1, which leaves the stock sequential path untouched.
"""

import asyncio
import json
import math
import os
import re
import shutil
import time
from typing import List, Optional, Tuple

from loguru import logger

from module import pyrogram_guard
from utils.atomic_write import write_text_atomic

#: One range == one in-flight GetFile request; going past this mostly buys
#: FloodWait instead of bandwidth.
MAX_CHUNK_CONCURRENCY = 16

#: Sidecar next to ``<file>.temp`` that stores the per-range byte counts.
STATE_SUFFIX = pyrogram_guard.PARALLEL_STATE_SUFFIX

#: Minimum seconds between two sidecar writes while chunks are arriving.
STATE_SAVE_INTERVAL = 2.0

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


def _write_state(state_path: str, state: List[int]) -> None:
    """Persist the per-range byte counts (best effort, never fatal)."""
    if not state_path:
        return
    try:
        write_text_atomic(state_path, json.dumps(list(state)))
    except OSError as error:
        logger.debug(f"could not save download state {state_path}: {error}")


def _drop_state(state_path: str) -> None:
    """Remove the sidecar after a finished download or after giving up on it."""
    if not state_path or not os.path.exists(state_path):
        return
    try:
        os.remove(state_path)
    except OSError as error:
        logger.debug(f"could not remove download state {state_path}: {error}")


def _load_state(
    state_path: str, ranges: List[Tuple[int, int]], file_size: int
) -> Optional[List[int]]:
    """Read and validate the sidecar; ``None`` means "cannot be trusted"."""
    try:
        with open(state_path, encoding="utf-8") as state_file:
            raw = json.load(state_file)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, list) or len(raw) != len(ranges):
        return None

    chunk_size = pyrogram_guard.CHUNK_SIZE
    state: List[int] = []
    for value, (start, end) in zip(raw, ranges):
        expected = min(end * chunk_size, file_size) - start * chunk_size
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        if value < 0 or value > expected:
            return None
        state.append(value)
    return state


class _ChunkWriter:
    """Write chunks at absolute offsets and publish a monotonic byte total."""

    def __init__(
        self,
        file_obj,
        file_size: int,
        progress,
        progress_args,
        state: Optional[List[int]] = None,
        state_path: str = "",
    ):
        self._file = file_obj
        self._size = file_size
        self._progress = progress
        self._progress_args = progress_args
        self._write_lock = asyncio.Lock()
        # The progress callback derives the speed from deltas, so it must never
        # see a smaller total after a bigger one.
        self._progress_lock = asyncio.Lock()
        self._state = state if state is not None else []
        self._state_path = state_path
        self._last_save = 0.0
        # Bytes already on disk from a previous attempt count as progress too.
        self.received = sum(self._state)
        self._reported = self.received

    def state_of(self, index: int) -> int:
        """Bytes of range ``index`` a previous attempt already wrote."""
        if not self._state or index < 0 or index >= len(self._state):
            return 0
        return self._state[index]

    async def write(self, offset: int, chunk: bytes) -> None:
        """Store ``chunk`` at ``offset`` and report the aggregated progress."""
        async with self._write_lock:
            self._file.seek(offset)
            self._file.write(chunk)
            self.received += len(chunk)

        if self._progress is None:
            return
        async with self._progress_lock:
            received = max(self._reported, self.received)
            self._reported = received
            await self._progress(received, self._size, *self._progress_args)

    def mark(self, index: int, received: int) -> None:
        """Record how much of range ``index`` is on disk, throttled."""
        if not self._state:
            return
        self._state[index] = received
        now = time.monotonic()
        if now - self._last_save >= STATE_SAVE_INTERVAL:
            self._last_save = now
            _write_state(self._state_path, self._state)

    def flush_state(self) -> None:
        """Write the sidecar immediately (failure or cancellation)."""
        if self._state:
            _write_state(self._state_path, self._state)


async def _download_range(
    client,
    file_id,
    file_size: int,
    index: int,
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

    # Bytes of this range that a previous attempt already put on disk.
    received = min(max(writer.state_of(index), 0), expected)
    stalled = 0
    flood_waits = 0
    while received < expected:
        absolute = range_start + received
        chunk_index = absolute // chunk_size
        # A previous attempt may have stopped mid-chunk, so the first chunk of a
        # resumed stream can carry a few bytes that are already on disk.
        skip = absolute - chunk_index * chunk_size
        progressed = False

        try:
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
                writer.mark(index, received)
                progressed = True
                flood_waits = 0
                if received >= expected:
                    break
        except pyrogram_guard.flood_errors() as flood:
            # Same policy as the sequential guard: wait instead of throwing the
            # partial range away.
            wait = pyrogram_guard.flood_wait_seconds(flood)
            flood_waits += 1
            if flood_waits > pyrogram_guard.MAX_FLOOD_WAITS_PER_STREAM:
                raise pyrogram_guard.DownloadIncomplete(received, expected) from flood
            logger.warning(
                f"{label}: flood wait {wait:g}s on range "
                f"{start_chunk}-{end_chunk} ({type(flood).__name__})"
            )
            await asyncio.sleep(wait + pyrogram_guard.FLOOD_WAIT_MARGIN)
            continue

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


async def _run_ranges(
    client, file_id, file_size: int, ranges, writer: _ChunkWriter, label: str
):
    tasks = [
        asyncio.ensure_future(
            _download_range(
                client, file_id, file_size, index, start, end, writer, label
            )
        )
        for index, (start, end) in enumerate(ranges)
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
    final path, on failure it returns ``None`` while ``FloodWait`` / cancellation
    propagate — so ``pyrogram_guard.guard_handle_download`` keeps turning a
    swallowed ``DownloadIncomplete`` into an exception.

    The sequential path goes through ``pyrogram_guard.resumable_handle_download``
    so a failed attempt keeps its ``.temp`` and the retry (or the next start)
    continues instead of downloading the file again from zero. The parallel path
    keeps the same guarantee through the ``.temp.json`` sidecar.
    """

    resumable = pyrogram_guard.resumable_handle_download(original)

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
        if in_memory or not file_size:
            return await original(self, packet)
        if _concurrency <= 1:
            # One stream: let it resume from the partial file.
            return await resumable(self, packet)

        os.makedirs(directory, exist_ok=True)
        temp_file_path = (
            os.path.abspath(re.sub("\\\\", "/", os.path.join(directory, file_name)))
            + ".temp"
        )
        state_path = temp_file_path + STATE_SUFFIX
        label = f"[{file_name}]"
        ranges = chunk_ranges(file_size, _concurrency)

        state: Optional[List[int]] = None
        if ranges and os.path.exists(temp_file_path):
            state = _load_state(state_path, ranges, file_size)
        if state is None:
            # No trustworthy sidecar (first attempt, or a version that did not
            # write one): start over instead of splicing unknown bytes.
            state = [0] * len(ranges)
            written = 0
        else:
            chunk_size = pyrogram_guard.CHUNK_SIZE
            written = max(
                (
                    start * chunk_size + received
                    for (start, _end), received in zip(ranges, state)
                ),
                default=0,
            )
            on_disk = (
                os.path.getsize(temp_file_path)
                if os.path.exists(temp_file_path)
                else 0
            )
            if on_disk < written:
                # The file is shorter than the sidecar claims (crash, cleanup,
                # manual truncation): the state cannot be trusted.
                logger.debug(f"{label}: temp file shorter than its state, restarting")
                state = [0] * len(ranges)
                written = 0

        if ranges and sum(state) == file_size and written >= file_size:
            # Complete leftover from a run that died between the last write and
            # the rename: salvage it instead of downloading everything again.
            final_path = os.path.splitext(temp_file_path)[0]
            with open(temp_file_path, "r+b") as file_obj:
                file_obj.truncate(file_size)
            _drop_state(state_path)
            shutil.move(temp_file_path, final_path)
            return final_path

        writer: Optional[_ChunkWriter] = None
        received = 0
        try:
            resume = sum(state) > 0
            with open(temp_file_path, "r+b" if resume else "wb") as file_obj:
                writer = _ChunkWriter(
                    file_obj,
                    file_size,
                    progress,
                    progress_args,
                    state=state,
                    state_path=state_path,
                )
                await _run_ranges(self, file_id, file_size, ranges, writer, label)
                received = writer.received
        except BaseException as error:  # pylint: disable = W0703
            # Keep ".temp" + sidecar so the retry resumes where this attempt
            # stopped; only an explicit deletion should throw the bytes away.
            if writer is not None:
                writer.flush_state()
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
            writer.flush_state()
            logger.warning(
                f"{label} parallel download incomplete: {received}/{file_size}"
            )
            pyrogram_guard.record_swallowed_error(
                pyrogram_guard.DownloadIncomplete(received, file_size)
            )
            return None

        final_path = os.path.splitext(temp_file_path)[0]
        _drop_state(state_path)
        shutil.move(temp_file_path, final_path)
        return final_path

    handle_download.__tdl_parallel__ = True
    return handle_download
