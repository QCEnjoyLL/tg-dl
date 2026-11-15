"""Regression tests for rclone command construction."""

import unittest
from unittest import mock

from module.cloud_drive import CloudDrive, CloudDriveConfig


class FakeStdout:
    """Empty async stdout stream."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class FakeProcess:
    """Minimal asyncio subprocess stand-in."""

    def __init__(self, returncode: int = 0):
        self.stdout = FakeStdout()
        self.returncode = returncode

    async def wait(self):
        return self.returncode


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

    def test_mkdir_args_are_not_shell_strings(self):
        args = CloudDrive.rclone_mkdir_args("/usr/bin/rclone", "remote:dir $(id)")
        self.assertEqual(
            args, ["/usr/bin/rclone", "mkdir", "remote:dir $(id)"]
        )
        self.assertIsInstance(args, list)


class RcloneUploadTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_upload_uses_exec_not_shell(self):
        config = CloudDriveConfig(
            enable_upload_file=True,
            after_upload_file_delete=False,
            rclone_path="/usr/bin/rclone",
            remote_dir="remote:",
        )
        process = FakeProcess()

        with mock.patch.object(
            CloudDrive, "rclone_mkdir", new=mock.AsyncMock(return_value=True)
        ), mock.patch(
            "module.cloud_drive.asyncio.create_subprocess_exec",
            new=mock.AsyncMock(return_value=process),
        ) as exec_mock:
            ok = await CloudDrive.rclone_upload_file(
                config, "/save", "/save/channel/file name.mp4"
            )

        self.assertTrue(ok)
        args, kwargs = exec_mock.await_args
        self.assertEqual(args[0], "/usr/bin/rclone")
        self.assertEqual(args[1], "copyto")
        self.assertIn("/save/channel/file name.mp4", args)
        self.assertNotIn("shell", kwargs)
        self.assertNotIn("--size-only", args)
