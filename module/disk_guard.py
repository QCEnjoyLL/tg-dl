"""Pause downloads while the target volume is nearly full.

Uploads now run outside the download workers, so nothing inherently limits how
far ahead the downloader can run: without a guard a slow cloud upload would let
``save_path`` grow until the disk is full, and a download killed by ``ENOSPC``
leaves a broken temp file behind.

The gate is checked before a file starts. It waits while the volume has less than
``reserve`` bytes free (plus room for the file itself), logs a warning when it
pauses and another when it resumes, and re-checks on a timer. Uploads delete the
local copy when they succeed, so the wait ends by itself once the backlog drains.

Two paths are watched because the download path and ``save_path`` may live on
different volumes: ``.temp`` files are written next to the download target, and
the finished file is moved to ``save_path``.
"""

import asyncio
from contextlib import asynccontextmanager
import os
import shutil
from typing import Callable, List, Optional

from loguru import logger

#: How often a paused gate re-checks the volume.
DEFAULT_POLL_SECONDS = 15.0

#: Resume only once the volume gains 20% more than the reserve, so the gate does
#: not flap between "just enough" and "not enough" while files are arriving.
RECOVERY_RATIO = 1.2

#: Smallest hysteresis step, for volumes where 20% of the reserve is tiny.
MIN_RECOVERY_SLACK = 256 * 1024 * 1024


class DiskSpaceError(Exception):
    """The file can never fit on the target volume."""


class DiskWaitCancelled(Exception):
    """Waiting for disk space was cancelled."""


class DiskSpaceGate:
    """Wait until the watched volumes have room for the next download."""

    def __init__(
        self,
        paths: List[str],
        reserve_bytes: int,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        usage_fn: Optional[Callable] = None,
    ):
        self._paths = [path for path in paths if path]
        self.reserve_bytes = max(int(reserve_bytes), 0)
        self._poll_seconds = max(float(poll_seconds), 0.01)
        self._usage_fn = usage_fn or shutil.disk_usage
        self._low = False
        self._inflight_bytes: int = 0
        self._waiters: int = 0

    @property
    def disabled(self) -> bool:
        """True when the guard is off (reserve 0) or there is nothing to watch."""
        return self.reserve_bytes <= 0 or not self._paths

    @property
    def is_low(self) -> bool:
        """Whether downloads are currently paused for lack of space."""
        return self._low

    def _usage_for(self, path: str):
        """Usage for ``path``, falling back to its nearest existing parent.

        ``save_path`` may not exist before the first finished file is moved
        there; the volume it will live on is still the one we must watch.
        """
        candidate = path
        while True:
            try:
                return self._usage_fn(candidate)
            except OSError:
                parent = os.path.dirname(candidate)
                if parent == candidate:
                    return None
                candidate = parent

    def _tightest_usage(self):
        """The usage record with the least free space, or ``None``."""
        tightest = None
        for path in self._paths:
            usage = self._usage_for(path)
            if usage is None:
                # A missing/unmounted path must not stall every download.
                continue
            if tightest is None or usage.free < tightest.free:
                tightest = usage
        return tightest

    def free_bytes(self) -> Optional[int]:
        """Free bytes on the tightest watched volume (``None`` if unreadable)."""
        usage = self._tightest_usage()
        return int(usage.free) if usage else None

    def _resume_threshold(
        self, need_bytes: int, total_bytes: Optional[int] = None
    ) -> int:
        target = (
            self.reserve_bytes
            + need_bytes
            + max(
                int(self.reserve_bytes * (RECOVERY_RATIO - 1.0)),
                MIN_RECOVERY_SLACK,
            )
        )
        if total_bytes is not None:
            # Never demand more free space than the volume physically holds,
            # otherwise a large file that fits (reserve + need <= total) would
            # deadlock waiting on the hysteresis margin.
            target = min(target, int(total_bytes))
        return target

    def status(self) -> dict:
        """Snapshot for the Web UI / logs."""
        usage = self._tightest_usage()
        return {
            "free": int(usage.free) if usage else None,
            "total": int(usage.total) if usage else None,
            "reserve": self.reserve_bytes,
            "paused": bool(self._low),
            "inflight": self._inflight_bytes,
            "paths": list(self._paths),
        }

    async def _sleep_poll(self, should_cancel: Optional[Callable[[], bool]]) -> None:
        if should_cancel is None:
            await asyncio.sleep(self._poll_seconds)
            return
        remaining = self._poll_seconds
        while remaining > 0:
            if should_cancel():
                raise DiskWaitCancelled("disk wait cancelled")
            step = min(remaining, 0.5)
            await asyncio.sleep(step)
            remaining -= step
        if should_cancel():
            raise DiskWaitCancelled("disk wait cancelled")

    async def _acquire(
        self,
        need_bytes: int = 0,
        *,
        reserve: bool = False,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> int:
        if self.disabled:
            self._low = False
            return 0
        if should_cancel and should_cancel():
            raise DiskWaitCancelled("disk wait cancelled")

        usage = self._tightest_usage()
        if usage is None:
            logger.warning("disk guard cannot read any watched path; not pausing")
            return 0

        need = max(int(need_bytes), 0)
        if self.reserve_bytes + need > usage.total:
            raise DiskSpaceError(
                f"文件需要 {need} 字节，但该磁盘总共只有 {usage.total} 字节"
                f"（还需预留 {self.reserve_bytes} 字节），无法下载"
            )

        waiting = False
        try:
            while True:
                if should_cancel and should_cancel():
                    raise DiskWaitCancelled("disk wait cancelled")
                if self.disabled:
                    self._low = False
                    return 0

                usage = self._tightest_usage()
                if usage is None:
                    return 0
                free = int(usage.free)
                effective_free = max(free - self._inflight_bytes, 0)

                if not self._low and effective_free >= self.reserve_bytes + need:
                    if reserve:
                        self._inflight_bytes += need
                        return need
                    return 0
                if self._low and effective_free >= self._resume_threshold(
                    need, usage.total
                ):
                    self._low = False
                    logger.info(
                        f"磁盘空间已恢复（剩余 {free} 字节，预留 {self.reserve_bytes}），"
                        f"继续下载"
                    )
                    if reserve:
                        self._inflight_bytes += need
                        return need
                    return 0

                if not waiting:
                    self._waiters += 1
                    waiting = True
                if not self._low:
                    self._low = True
                    logger.warning(
                        f"磁盘空间不足：剩余 {free} 字节，"
                        f"低于预留 {self.reserve_bytes} 字节，暂停下载并等待上传释放空间"
                        f"（每 {self._poll_seconds:g} 秒检查一次）"
                    )
                await self._sleep_poll(should_cancel)
        finally:
            if waiting:
                self._waiters = max(self._waiters - 1, 0)
                if self._waiters == 0:
                    self._low = False

    async def wait_for(
        self,
        need_bytes: int = 0,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> None:
        """Block until the volumes can hold ``need_bytes`` more plus the reserve.

        Raises ``DiskSpaceError`` when the file cannot fit even on an empty
        volume, so the task fails with an actionable reason instead of waiting
        forever.
        """
        await self._acquire(need_bytes, reserve=False, should_cancel=should_cancel)

    @asynccontextmanager
    async def hold(
        self,
        need_bytes: int = 0,
        should_cancel: Optional[Callable[[], bool]] = None,
    ):
        """Reserve ``need_bytes`` while a download runs, releasing it on exit."""
        reserved = await self._acquire(
            need_bytes, reserve=True, should_cancel=should_cancel
        )
        try:
            yield
        finally:
            if reserved:
                self._inflight_bytes = max(self._inflight_bytes - reserved, 0)


#: The gate created at startup; the Web UI reads its snapshot through
#: :func:`status`. ``None`` means "no guard configured".
_gate: Optional[DiskSpaceGate] = None


def set_gate(gate: Optional[DiskSpaceGate]) -> None:
    """Install (or clear) the process-wide gate."""
    # pylint: disable = W0603
    global _gate
    _gate = gate


def get_gate() -> Optional[DiskSpaceGate]:
    """The active gate, if any."""
    return _gate


def set_reserve(reserve_bytes: int) -> None:
    """Update the reserve of the active gate (used by live config reload)."""
    gate = _gate
    if gate is not None:
        gate.reserve_bytes = max(int(reserve_bytes), 0)


def status() -> dict:
    """Snapshot for the Web UI; an inactive guard reports ``enabled: False``."""
    gate = _gate
    if gate is None:
        return {"enabled": False, "paused": False}
    return {"enabled": not gate.disabled, **gate.status()}


async def wait_for_space(
    need_bytes: int = 0, should_cancel: Optional[Callable[[], bool]] = None
) -> None:
    """Wait for room before downloading a file of ``need_bytes``."""
    gate = _gate
    if gate is None:
        return
    await gate.wait_for(need_bytes, should_cancel=should_cancel)


@asynccontextmanager
async def reserve_space(
    need_bytes: int = 0, should_cancel: Optional[Callable[[], bool]] = None
):
    """Reserve ``need_bytes`` while a download runs (no-op without a gate)."""
    gate = _gate
    if gate is None:
        yield
        return
    async with gate.hold(need_bytes, should_cancel=should_cancel):
        yield
