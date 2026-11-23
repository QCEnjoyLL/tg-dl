"""Tests for the disk-space gate that pauses downloads."""

import asyncio
import unittest
from collections import namedtuple
from unittest import mock

from module import disk_guard

Usage = namedtuple("Usage", "total used free")
GB = 1024**3


class FakeVolume:
    """``shutil.disk_usage`` stand-in with a scripted free-space sequence."""

    def __init__(self, total, frees):
        self.total = total
        self._frees = list(frees)

    def __call__(self, _path):
        free = self._frees.pop(0) if len(self._frees) > 1 else self._frees[0]
        return Usage(self.total, self.total - free, free)


class DiskSpaceGateTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_without_a_reserve(self):
        gate = disk_guard.DiskSpaceGate(
            ["/dl"], 0, poll_seconds=1, usage_fn=FakeVolume(GB, [0])
        )

        self.assertTrue(gate.disabled)
        # Returns immediately even though the volume is empty.
        await gate.wait_for(GB)
        self.assertFalse(gate.is_low)

    async def test_waits_when_space_is_below_the_reserve(self):
        volume = FakeVolume(10 * GB, [GB, GB, 6 * GB])
        gate = disk_guard.DiskSpaceGate(
            ["/dl"], 2 * GB, poll_seconds=0.01, usage_fn=volume
        )

        with (
            mock.patch.object(disk_guard.logger, "warning") as warn_mock,
            mock.patch.object(disk_guard.logger, "info") as info_mock,
        ):
            await gate.wait_for(GB)

        warn_mock.assert_called()  # paused
        info_mock.assert_called()  # resumed
        self.assertFalse(gate.is_low)
        # The gate consumed the low samples before letting the download through.
        self.assertEqual(volume._frees, [6 * GB])  # pylint: disable = W0212

    async def test_resume_needs_the_hysteresis_margin(self):
        # After pausing, 3 GB is enough for reserve + file but not for the 20%
        # recovery margin, so the gate keeps waiting instead of flapping.
        volume = FakeVolume(10 * GB, [GB, 3 * GB, 5 * GB])
        gate = disk_guard.DiskSpaceGate(
            ["/dl"], 2 * GB, poll_seconds=0.01, usage_fn=volume
        )

        await gate.wait_for(GB)

        self.assertFalse(gate.is_low)
        self.assertEqual(volume._frees, [5 * GB])  # pylint: disable = W0212

    async def test_impossible_file_raises_instead_of_waiting_forever(self):
        gate = disk_guard.DiskSpaceGate(
            ["/dl"], 2 * GB, poll_seconds=0.01, usage_fn=FakeVolume(GB, [GB])
        )

        with self.assertRaises(disk_guard.DiskSpaceError):
            await gate.wait_for(GB)

    async def test_unreadable_path_does_not_block_downloads(self):
        def broken(_path):
            raise OSError("not mounted")

        gate = disk_guard.DiskSpaceGate(
            ["nope"], 2 * GB, poll_seconds=0.01, usage_fn=broken
        )

        await gate.wait_for(GB)
        self.assertFalse(gate.is_low)
        self.assertIsNone(gate.free_bytes())

    async def test_uses_the_tightest_of_several_volumes(self):
        usage = {"/temp": Usage(GB, 0, 0.5 * GB), "/save": Usage(GB, 0, 5 * GB)}
        gate = disk_guard.DiskSpaceGate(
            ["/temp", "/save"], 2 * GB, poll_seconds=0.01, usage_fn=lambda p: usage[p]
        )

        self.assertEqual(gate.free_bytes(), int(0.5 * GB))

    async def test_missing_save_path_watches_its_parent_volume(self):
        # save_path may not exist before the first finished file is moved in.
        def usage(path):
            if path != "/":
                raise OSError("missing")
            return Usage(10 * GB, 0, 5 * GB)

        gate = disk_guard.DiskSpaceGate(
            ["/save/not/created/yet"], 2 * GB, poll_seconds=0.01, usage_fn=usage
        )

        self.assertEqual(gate.free_bytes(), 5 * GB)
        await gate.wait_for(GB)
        self.assertFalse(gate.is_low)

    def test_status_reports_the_pause_state(self):
        gate = disk_guard.DiskSpaceGate(
            ["/dl"], 2 * GB, poll_seconds=1, usage_fn=FakeVolume(10 * GB, [GB])
        )
        gate._low = True  # pylint: disable = W0212

        snapshot = gate.status()

        self.assertTrue(snapshot["paused"])
        self.assertEqual(snapshot["reserve"], 2 * GB)
        self.assertEqual(snapshot["free"], GB)
        self.assertEqual(snapshot["paths"], ["/dl"])

    async def test_resume_threshold_is_capped_at_volume_total(self):
        # A 7.9 GB file on a 10 GB disk with 2 GB reserve fits (9.9 <= 10),
        # so the hysteresis slack must not demand > 10 GB free and deadlock.
        need = int(7.9 * GB)
        volume = FakeVolume(10 * GB, [8 * GB, 10 * GB])
        gate = disk_guard.DiskSpaceGate(
            ["/dl"], 2 * GB, poll_seconds=0.01, usage_fn=volume
        )

        await asyncio.wait_for(gate.wait_for(need), timeout=1.0)
        self.assertFalse(gate.is_low)

    async def test_should_cancel_aborts_wait_and_clears_low_flag(self):
        gate = disk_guard.DiskSpaceGate(
            ["/dl"], 2 * GB, poll_seconds=0.01, usage_fn=FakeVolume(10 * GB, [GB])
        )
        cancelled = False

        async def trigger_cancel():
            nonlocal cancelled
            await asyncio.sleep(0.03)
            cancelled = True

        canceller = asyncio.create_task(trigger_cancel())
        try:
            with self.assertRaises(disk_guard.DiskWaitCancelled):
                await gate.wait_for(GB, should_cancel=lambda: cancelled)
        finally:
            await canceller

        self.assertFalse(gate.is_low)

    async def test_disabling_reserve_unblocks_waiting_task(self):
        gate = disk_guard.DiskSpaceGate(
            ["/dl"], 2 * GB, poll_seconds=0.01, usage_fn=FakeVolume(10 * GB, [GB])
        )

        async def disable_later():
            await asyncio.sleep(0.03)
            gate.reserve_bytes = 0

        updater = asyncio.create_task(disable_later())
        try:
            await asyncio.wait_for(gate.wait_for(GB), timeout=1.0)
        finally:
            await updater

        self.assertFalse(gate.is_low)

    async def test_hold_reserves_inflight_bytes_across_concurrent_downloads(self):
        # Volume has 5 GB free and 2 GB reserve -> 3 GB usable.
        # Two concurrent 2 GB downloads together need 4 GB, so the second must
        # wait until the first releases its reservation.
        free = 5 * GB
        gate = disk_guard.DiskSpaceGate(
            ["/dl"],
            2 * GB,
            poll_seconds=0.01,
            usage_fn=lambda _p: Usage(10 * GB, 10 * GB - free, free),
        )
        events = []

        async def first():
            async with gate.hold(2 * GB):
                events.append("first_acquired")
                self.assertEqual(gate.status()["inflight"], 2 * GB)
                await asyncio.sleep(0.05)
                events.append("first_releasing")

        async def second():
            await asyncio.sleep(0.01)
            async with gate.hold(2 * GB):
                events.append("second_acquired")

        await asyncio.wait_for(asyncio.gather(first(), second()), timeout=1.0)

        self.assertEqual(
            events, ["first_acquired", "first_releasing", "second_acquired"]
        )
        self.assertEqual(gate.status()["inflight"], 0)

    async def test_hold_releases_reservation_on_exception(self):
        gate = disk_guard.DiskSpaceGate(
            ["/dl"], 2 * GB, poll_seconds=0.01, usage_fn=FakeVolume(10 * GB, [6 * GB])
        )
        with self.assertRaises(RuntimeError):
            async with gate.hold(2 * GB):
                self.assertEqual(gate.status()["inflight"], 2 * GB)
                raise RuntimeError("boom")

        self.assertEqual(gate.status()["inflight"], 0)


class ModuleLevelGateTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        disk_guard.set_gate(None)

    def tearDown(self):
        disk_guard.set_gate(None)

    async def test_wait_for_space_is_a_no_op_without_a_gate(self):
        await disk_guard.wait_for_space(GB)
        self.assertEqual(disk_guard.status(), {"enabled": False, "paused": False})

    async def test_module_gate_is_used(self):
        gate = disk_guard.DiskSpaceGate(
            ["/dl"],
            2 * GB,
            poll_seconds=0.01,
            usage_fn=FakeVolume(10 * GB, [GB, 6 * GB]),
        )
        disk_guard.set_gate(gate)

        await disk_guard.wait_for_space(GB)

        self.assertTrue(disk_guard.status()["enabled"])

    async def test_reserve_space_is_no_op_without_gate_and_reserves_with_gate(self):
        async with disk_guard.reserve_space(GB):
            self.assertFalse(disk_guard.status()["enabled"])

        gate = disk_guard.DiskSpaceGate(
            ["/dl"], 2 * GB, poll_seconds=0.01, usage_fn=FakeVolume(10 * GB, [6 * GB])
        )
        disk_guard.set_gate(gate)
        async with disk_guard.reserve_space(GB):
            self.assertEqual(disk_guard.status()["inflight"], GB)
        self.assertEqual(disk_guard.status()["inflight"], 0)
