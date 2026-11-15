"""Regression tests for rclone command construction and upload verdicts."""

import unittest
from unittest import mock

from module.cloud_drive import (
    UPLOAD_EXISTS,
    UPLOAD_NEW,
    CloudDrive,
    CloudDriveConfig,
)


class FakeStdout:
    """Async stdout stream over a list of byte lines."""

    def __init__(self, lines=None):
        self._lines = list(lines or [])

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class FakeProcess:
    """Minimal asyncio subprocess stand-in."""

    def __init__(self, returncode: int = 0, lines=None):
        self.stdout = FakeStdout(lines)
        self.returncode = returncode

    async def wait(self):
        return self.returncode


def make_config(**overrides):
    options = {
        "enable_upload_file": True,
        "after_upload_file_delete": False,
        "rclone_path": "/usr/bin/rclone",
        "remote_dir": "remote:",
    }
    options.update(overrides)
    return CloudDriveConfig(**options)


class RcloneArgumentsTestCase(unittest.TestCase):
    def test_copyto_args_keep_attacker_controlled_text_as_one_argument(self):
        dangerous = "/tmp/a $(touch pwned) `id`;rm -rf / #.mp4"
        args = CloudDrive.rclone_copyto_args(
            "/usr/bin/rclone", dangerous, "remote:dir/a $(touch pwned).mp4"
        )

        self.assertEqual(args[0], "/usr/bin/rclone")
        self.assertEqual(args[1], "copyto")
        self.assertIn(dangerous, args)
        self.assertNotIn("shell", args)
        # The old command included this flag and deleted the local file after a
        # same-size-but-different remote object was treated as uploaded.
        self.assertNotIn("--size-only", args)
        self.assertIn("--no-traverse", args)
        # Per-file verdicts ("Copied (new)" / "Unchanged skipping") come from -v.
        self.assertIn("-v", args)

    def test_mkdir_args_are_not_shell_strings(self):
        args = CloudDrive.rclone_mkdir_args("/usr/bin/rclone", "remote:dir $(id)")
        self.assertEqual(
            args, ["/usr/bin/rclone", "mkdir", "remote:dir $(id)"]
        )
        self.assertIsInstance(args, list)


class RcloneUploadTestCase(unittest.IsolatedAsyncioTestCase):
    async def upload(self, process):
        with mock.patch.object(
            CloudDrive, "rclone_mkdir", new=mock.AsyncMock(return_value=True)
        ), mock.patch(
            "module.cloud_drive.asyncio.create_subprocess_exec",
            new=mock.AsyncMock(return_value=process),
        ) as exec_mock:
            result = await CloudDrive.rclone_upload_file(
                make_config(), "/save", "/save/channel/file name.mp4"
            )
        return result, exec_mock

    async def test_upload_uses_exec_not_shell(self):
        result, exec_mock = await self.upload(FakeProcess())

        self.assertEqual(result, UPLOAD_NEW)
        args, kwargs = exec_mock.await_args
        self.assertEqual(args[0], "/usr/bin/rclone")
        self.assertEqual(args[1], "copyto")
        self.assertIn("/save/channel/file name.mp4", args)
        self.assertNotIn("shell", kwargs)
        self.assertNotIn("--size-only", args)

    async def test_new_file_is_reported_as_uploaded(self):
        process = FakeProcess(
            lines=[
                b"Transferred:   12.3 MiB / 12.3 MiB, 100%, 0 B/s, ETA -\n",
                b"INFO  : file name.mp4: Copied (new)\n",
            ]
        )
        result, _ = await self.upload(process)

        self.assertEqual(result, UPLOAD_NEW)

    async def test_existing_file_is_reported_as_skipped(self):
        process = FakeProcess(
            lines=[b"INFO  : file name.mp4: Unchanged skipping\n"]
        )
        result, _ = await self.upload(process)

        self.assertEqual(result, UPLOAD_EXISTS)

    async def test_failed_upload_returns_none(self):
        process = FakeProcess(returncode=1, lines=[b"ERROR : something broke\n"])
        result, _ = await self.upload(process)

        self.assertIsNone(result)
