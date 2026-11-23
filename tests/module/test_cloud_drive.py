"""Regression tests for rclone command construction and upload verdicts."""

import asyncio
import os
import tempfile
import threading
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


class RemoteDirTestCase(unittest.TestCase):
    """The remote path must never leak the local absolute path."""

    def test_relative_dir_from_save_path(self):
        config = make_config(remote_dir="115:Telegram")
        remote_dir = CloudDrive.remote_dir_for(
            config, "/app/downloads", "/app/downloads/chan/2026_04/f.mp4"
        )
        self.assertEqual(remote_dir, "115:Telegram/chan/2026_04/")

    def test_trailing_slash_in_save_path_is_irrelevant(self):
        config = make_config(remote_dir="115:Telegram")
        remote_dir = CloudDrive.remote_dir_for(
            config, "/app/downloads/", "/app/downloads/chan/f.mp4"
        )
        self.assertEqual(remote_dir, "115:Telegram/chan/")

    def test_windows_paths_are_normalized(self):
        config = make_config(remote_dir="r:")
        remote_dir = CloudDrive.remote_dir_for(
            config, "C:\\dl", "C:\\dl\\chan\\f.mp4"
        )
        self.assertEqual(remote_dir, "r:/chan/")

    def test_file_outside_save_path_does_not_create_dot_dot_paths(self):
        config = make_config(remote_dir="r:")
        remote_dir = CloudDrive.remote_dir_for(
            config, "/app/downloads", "/tmp/other/f.mp4"
        )
        self.assertNotIn("..", remote_dir)
        self.assertTrue(remote_dir.startswith("r:/"))

    def test_duplicate_slashes_are_collapsed(self):
        config = make_config(remote_dir="115:Telegram/")
        remote_dir = CloudDrive.remote_dir_for(
            config, "/app/downloads", "/app/downloads/chan/f.mp4"
        )
        self.assertEqual(remote_dir, "115:Telegram/chan/")


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

    async def test_same_size_on_remote_skips_upload_entirely(self):
        # A freshly downloaded file has a new mtime, so rclone would re-upload
        # it even though the identical file is already on the drive. The
        # pre-check must prevent that (and keep the remote date untouched).
        with mock.patch.object(
            CloudDrive, "rclone_mkdir", new=mock.AsyncMock(return_value=True)
        ), mock.patch.object(
            CloudDrive,
            "rclone_remote_stat",
            new=mock.AsyncMock(return_value={"Size": 123}),
        ), mock.patch(
            "module.cloud_drive.os.path.getsize", return_value=123
        ), mock.patch(
            "module.cloud_drive.asyncio.create_subprocess_exec",
            new=mock.AsyncMock(),
        ) as exec_mock:
            # hash_check off: exercise the size-only verdict this test is about.
            result = await CloudDrive.rclone_upload_file(
                make_config(hash_check=False), "/save", "/save/f.mp4"
            )

        self.assertEqual(result, UPLOAD_EXISTS)
        exec_mock.assert_not_called()

    async def test_different_size_on_remote_is_uploaded(self):
        process = FakeProcess(lines=[b"INFO  : f.mp4: Copied (new)\n"])
        with mock.patch.object(
            CloudDrive, "rclone_mkdir", new=mock.AsyncMock(return_value=True)
        ), mock.patch.object(
            CloudDrive,
            "rclone_remote_stat",
            new=mock.AsyncMock(
                side_effect=[{"Size": 1}, {"Size": 123, "ModTime": "2026-11-16T04:00:00Z"}]
            ),
        ), mock.patch(
            "module.cloud_drive.os.path.getsize", return_value=123
        ), mock.patch(
            "module.cloud_drive.asyncio.create_subprocess_exec",
            new=mock.AsyncMock(return_value=process),
        ):
            result = await CloudDrive.rclone_upload_file(
                make_config(), "/save", "/save/f.mp4"
            )

        self.assertEqual(result, UPLOAD_NEW)

    async def test_server_side_dedupe_is_reported_as_existing(self):
        # The drive kept its old object (3 days old) after the upload: rclone
        # says "Copied (new)" but the content was deduplicated, so the honest
        # answer is "already on the remote".
        process = FakeProcess(lines=[b"INFO  : f.mp4: Copied (new)\n"])
        with mock.patch.object(
            CloudDrive, "rclone_mkdir", new=mock.AsyncMock(return_value=True)
        ), mock.patch.object(
            CloudDrive,
            "rclone_remote_stat",
            new=mock.AsyncMock(
                side_effect=[
                    None,
                    {"Size": 123, "ModTime": "2026-11-13T10:00:00Z"},
                ]
            ),
        ), mock.patch(
            "module.cloud_drive.os.path.getsize", return_value=123
        ), mock.patch(
            "module.cloud_drive.asyncio.create_subprocess_exec",
            new=mock.AsyncMock(return_value=process),
        ):
            result = await CloudDrive.rclone_upload_file(
                make_config(), "/save", "/save/f.mp4"
            )

        self.assertEqual(result, UPLOAD_EXISTS)

    def test_remote_time_parser(self):
        parse = CloudDrive._parse_remote_time

        utc = parse("2026-11-13T10:00:00Z")
        self.assertIsNotNone(utc)
        # Backends without modtime support report the zero time.
        self.assertIsNone(parse("0001-01-01T00:00:00Z"))
        self.assertIsNone(parse(None))
        offset = parse("2026-11-13T10:00:00+08:00")
        self.assertIsNotNone(offset)
        self.assertAlmostEqual(utc - offset, 8 * 3600, delta=1)


class RemoteContentCheckTestCase(unittest.IsolatedAsyncioTestCase):
    """A same-size remote object is only trusted when the bytes match.

    The local file is deleted right after an "already uploaded" verdict, so a
    name+size match alone (a different file with the same length) would lose
    both copies.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.file_path = os.path.join(self._tmp.name, "file.mp4")
        with open(self.file_path, "wb") as handle:
            handle.write(b"same bytes here")
        self.size = os.path.getsize(self.file_path)
        self.local_hash = CloudDrive.local_md5(self.file_path)

    async def _upload(self, config, remote_hash, stat_size=None, delete=True):
        process = FakeProcess(
            lines=[b"INFO  : file.mp4: Copied (replaced existing)\n"]
        )
        with mock.patch.object(
            CloudDrive, "rclone_mkdir", new=mock.AsyncMock(return_value=True)
        ), mock.patch.object(
            CloudDrive,
            "rclone_remote_stat",
            new=mock.AsyncMock(
                return_value={"Size": self.size if stat_size is None else stat_size}
            ),
        ), mock.patch.object(
            CloudDrive,
            "rclone_remote_hash",
            new=mock.AsyncMock(return_value=remote_hash),
        ) as hash_mock, mock.patch(
            "module.cloud_drive.asyncio.create_subprocess_exec",
            new=mock.AsyncMock(return_value=process),
        ) as exec_mock:
            config.after_upload_file_delete = delete
            result = await CloudDrive.rclone_upload_file(
                config, self._tmp.name, self.file_path
            )
        return result, hash_mock, exec_mock

    async def test_matching_hash_skips_the_upload(self):
        result, hash_mock, exec_mock = await self._upload(
            make_config(), self.local_hash
        )

        self.assertEqual(result, UPLOAD_EXISTS)
        hash_mock.assert_awaited()
        exec_mock.assert_not_called()
        # Identical content: deleting the local copy is safe.
        self.assertFalse(os.path.exists(self.file_path))

    async def test_different_hash_reuploads_and_keeps_the_local_copy(self):
        result, _, exec_mock = await self._upload(
            make_config(), "ffffffffffffffffffffffffffffffff", delete=False
        )

        self.assertEqual(result, UPLOAD_NEW)
        exec_mock.assert_called()
        self.assertTrue(os.path.exists(self.file_path))

    async def test_unavailable_hash_falls_back_to_the_size_match(self):
        result, _, exec_mock = await self._upload(make_config(), None)

        # Degrades to the previous size-only verdict instead of failing.
        self.assertEqual(result, UPLOAD_EXISTS)
        exec_mock.assert_not_called()

    async def test_hash_check_can_be_disabled(self):
        result, hash_mock, exec_mock = await self._upload(
            make_config(hash_check=False), "ffffffffffffffffffffffffffffffff"
        )

        self.assertEqual(result, UPLOAD_EXISTS)
        hash_mock.assert_not_awaited()
        exec_mock.assert_not_called()

    def test_local_md5_matches_hashlib(self):
        import hashlib

        expected = hashlib.md5(b"same bytes here").hexdigest()
        self.assertEqual(CloudDrive.local_md5(self.file_path), expected)


class _HangingStdout:
    def __init__(self, started: asyncio.Event):
        self._started = started

    def __aiter__(self):
        return self

    async def __anext__(self):
        self._started.set()
        await asyncio.sleep(60)
        raise StopAsyncIteration


class _CancellableProcess:
    def __init__(self, started: asyncio.Event):
        self.stdout = _HangingStdout(started)
        self.returncode = None
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


class RcloneCancellationTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_cancelling_upload_terminates_rclone_and_cleans_zip(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            src = os.path.join(tmp_dir, "video.mp4")
            with open(src, "wb") as f:
                f.write(b"payload")
            zip_path = os.path.join(tmp_dir, "video.zip")
            started = asyncio.Event()
            proc = _CancellableProcess(started)
            config = make_config(before_upload_file_zip=True)

            with mock.patch.object(
                CloudDrive, "rclone_mkdir", new=mock.AsyncMock(return_value=True)
            ), mock.patch.object(
                CloudDrive, "rclone_remote_stat", new=mock.AsyncMock(return_value=None)
            ), mock.patch(
                "module.cloud_drive.asyncio.create_subprocess_exec",
                new=mock.AsyncMock(return_value=proc),
            ):
                task = asyncio.create_task(
                    CloudDrive.rclone_upload_file(config, tmp_dir, src)
                )
                await asyncio.wait_for(started.wait(), timeout=1.0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

            self.assertTrue(proc.terminated)
            self.assertFalse(os.path.exists(zip_path))

    async def test_terminate_process_escalates_to_kill_on_timeout(self):
        proc = _CancellableProcess(asyncio.Event())
        proc.terminate = lambda: setattr(proc, "terminated", True)

        async def timeout_wait_for(coro, timeout=None):
            coro.close()
            raise asyncio.TimeoutError

        with mock.patch(
            "module.cloud_drive.asyncio.wait_for",
            side_effect=timeout_wait_for,
        ):
            await CloudDrive._terminate_process(proc)  # pylint: disable = W0212

        self.assertTrue(proc.terminated)
        self.assertTrue(proc.killed)


class CloudDriveConcurrencyAndAligoTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_uploads_deduplicate_rclone_mkdir(self):
        config = make_config()
        mkdir_calls = 0

        async def slow_mkdir(_cfg, _remote_dir):
            nonlocal mkdir_calls
            mkdir_calls += 1
            await asyncio.sleep(0.02)
            return True

        with (
            mock.patch.object(CloudDrive, "rclone_mkdir", side_effect=slow_mkdir),
            mock.patch.object(
                CloudDrive, "rclone_remote_stat", new=mock.AsyncMock(return_value=None)
            ),
            mock.patch(
                "module.cloud_drive.asyncio.create_subprocess_exec",
                side_effect=lambda *_a, **_kw: FakeProcess(),
            ),
        ):
            r1, r2 = await asyncio.gather(
                CloudDrive.rclone_upload_file(config, "/save", "/save/chan/a.mp4"),
                CloudDrive.rclone_upload_file(config, "/save", "/save/chan/b.mp4"),
            )

        self.assertEqual(r1, UPLOAD_NEW)
        self.assertEqual(r2, UPLOAD_NEW)
        self.assertEqual(mkdir_calls, 1)

    async def test_rclone_upload_zips_in_worker_thread_and_cleans_up(self):
        loop_thread = threading.get_ident()
        zip_threads = []
        orig_zip = CloudDrive.zip_file

        def spy_zip(path):
            zip_threads.append(threading.get_ident())
            return orig_zip(path)

        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "chan", "clip.mp4")
            os.makedirs(os.path.dirname(src), exist_ok=True)
            with open(src, "wb") as handle:
                handle.write(b"payload")
            zip_path = os.path.join(os.path.dirname(src), "clip.zip")
            config = make_config(before_upload_file_zip=True)

            with (
                mock.patch.object(
                    CloudDrive, "rclone_mkdir", new=mock.AsyncMock(return_value=True)
                ),
                mock.patch.object(
                    CloudDrive, "rclone_remote_stat", new=mock.AsyncMock(return_value=None)
                ),
                mock.patch.object(CloudDrive, "zip_file", side_effect=spy_zip),
                mock.patch(
                    "module.cloud_drive.asyncio.create_subprocess_exec",
                    return_value=FakeProcess(),
                ),
            ):
                result = await CloudDrive.rclone_upload_file(config, tmpdir, src)

            self.assertEqual(result, UPLOAD_NEW)
            self.assertEqual(len(zip_threads), 1)
            self.assertNotEqual(zip_threads[0], loop_thread)
            self.assertFalse(os.path.exists(zip_path))

    def test_aligo_upload_returns_none_when_folder_missing(self):
        fake_aligo = mock.MagicMock()
        fake_aligo.get_folder_by_path.return_value = None
        config = make_config(upload_adapter="aligo", before_upload_file_zip=True)
        config.aligo = fake_aligo

        with mock.patch.object(CloudDrive, "zip_file") as zip_mock:
            res = CloudDrive.aligo_upload_file(config, "/save", "/save/chan/a.mp4")

        self.assertIsNone(res)
        zip_mock.assert_not_called()
        fake_aligo.upload_files.assert_not_called()
