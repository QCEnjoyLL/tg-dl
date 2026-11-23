"""Run cloud-drive uploads outside the download workers.

Uploading a multi-GB file to a cloud drive takes minutes, and the upload used to
happen inside the worker that had just downloaded it: that worker could not pick
up the next queued message until the upload finished, so a slow drive made the
whole downloader look serial even with ``max_download_task: 4``.

The dispatcher starts one task per finished file. By default there is **no
concurrency limit** — an operator asked for uploads to run as fast as the drive
allows — but ``upload_concurrency`` can cap how many actually run at once (the
rest wait in memory, in submission order). The safety net against running out of
disk is :mod:`module.disk_guard`: downloads pause when the volume gets low and
resume once the uploads delete their local copies.

If the process stops while uploads are still queued, the affected rows stay in
the history as "仅本地" and the Web UI's re-upload button (or the next run's
normal upload pass) can send them later; the files are never deleted early.
"""

import asyncio
import threading
import weakref
from typing import Awaitable, Callable, Optional, Set, Tuple

from loguru import logger

#: 0 = unlimited. Module level so the Web UI can hot-reload it without holding
#: a reference to the dispatcher that lives in ``media_downloader``.
_limit = 0
_limit_lock = threading.Lock()


def get_limit() -> int:
    """How many uploads may run at once (``0`` = unlimited)."""
    with _limit_lock:
        return _limit


def set_limit(limit) -> int:
    """Change the limit; returns the value actually stored."""
    global _limit  # pylint: disable = W0603
    try:
        value = int(limit or 0)
    except (TypeError, ValueError):
        value = 0
    with _limit_lock:
        _limit = max(value, 0)
        return _limit


class UploadDispatcher:
    """Fire-and-forget upload jobs with an optional concurrency cap."""

    def __init__(self) -> None:
        self._tasks: Set[asyncio.Task] = set()
        #: loop -> (limit, semaphore). Rebuild when the hot-reloaded limit
        #: changes; jobs already running keep the semaphore they started with.
        self._semaphores: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Tuple[int, asyncio.Semaphore]]" = weakref.WeakKeyDictionary()

    @property
    def pending(self) -> int:
        """Uploads submitted that have not finished yet (running + queued)."""
        return len(self._tasks)

    def _semaphore(
        self, loop: asyncio.AbstractEventLoop
    ) -> Optional[asyncio.Semaphore]:
        limit = get_limit()
        if limit <= 0:
            self._semaphores.pop(loop, None)
            return None
        entry = self._semaphores.get(loop)
        if entry is None or entry[0] != limit:
            entry = (limit, asyncio.Semaphore(limit))
            self._semaphores[loop] = entry
        return entry[1]

    def submit(self, factory: Callable[[], Awaitable[None]]) -> bool:
        """Start one upload now (or queue it when the cap is reached).

        ``factory`` is a zero-argument callable returning the coroutine, so a
        task can be created without awaiting anything from the caller.
        Returns whether the job was scheduled on a running event loop.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - only outside an event loop
            logger.warning("upload dispatcher used without a running event loop")
            return False

        task = loop.create_task(self._run(factory, self._semaphore(loop)))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _run(
        self,
        factory: Callable[[], Awaitable[None]],
        semaphore: Optional[asyncio.Semaphore],
    ) -> None:
        try:
            if semaphore is None:
                await factory()
                return
            async with semaphore:
                await factory()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # pylint: disable = W0703
            # One failed upload must never take down the dispatcher or the
            # download workers; the file stays on disk for a retry.
            logger.exception(f"background upload failed: {error}")

    async def drain(self, timeout: float = 0.0) -> int:
        """Wait for running uploads; returns how many had to be cancelled.

        A non-positive ``timeout`` waits for everything (used by tests and by a
        one-shot run); a positive one bounds shutdown so ``Ctrl+C``/SIGINT does
        not hang on a stalled drive.
        """
        pending = list(self._tasks)
        if not pending:
            return 0

        if timeout and timeout > 0:
            _done, still_running = await asyncio.wait(pending, timeout=timeout)
            for task in still_running:
                task.cancel()
            if still_running:
                await asyncio.gather(*still_running, return_exceptions=True)
                logger.warning(
                    f"{len(still_running)} upload(s) still running at shutdown; "
                    f"files kept locally for the next retry"
                )
            return len(still_running)

        await asyncio.gather(*pending, return_exceptions=True)
        return 0

    def cancel_all(self) -> None:
        """Cancel every in-flight upload (the local files are kept)."""
        for task in list(self._tasks):
            task.cancel()
