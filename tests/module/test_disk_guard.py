"""Tests for the disk-space gate that pauses downloads."""

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

        with mock.patch.object(
            disk_guard.logger, "warning"
        ) as warn_mock, mock.patch.object(disk_guard.logger, "info") as info_mock:
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