"""provide upload cloud drive"""
import asyncio
import functools
import importlib
import inspect
import json
import os
import re
import time
from asyncio import subprocess
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from zipfile import ZipFile

from loguru import logger

from utils import platform

#: Upload results. ``UPLOAD_NEW`` means bytes were actually transferred,
#: ``UPLOAD_EXISTS`` means the remote already held an identical file and rclone
#: skipped it (still a success, but the operator deserves to know which one).
UPLOAD_NEW = "uploaded"
UPLOAD_EXISTS = "exists"

#: RFC 3339 timestamp as printed by ``rclone lsjson``.
_REMOTE_TIME_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})?"
)

#: A remote object older than this, discovered right after an "upload", means
#: the storage deduplicated the content and kept the original object.
_STALE_REMOTE_SECONDS = 6 * 3600


# pylint: disable = R0902
class CloudDriveConfig:
    """Rclone Config"""

    def __init__(
        self,
        enable_upload_file: bool = False,
        before_upload_file_zip: bool = False,
        after_upload_file_delete: bool = True,
        rclone_path: str = os.path.join(
            os.path.abspath("."), "rclone", f"rclone{platform.get_exe_ext()}"
        ),
        remote_dir: str = "",
        upload_adapter: str = "rclone",
    ):
        self.enable_upload_file = enable_upload_file
        self.before_upload_file_zip = before_upload_file_zip
        self.after_upload_file_delete = after_upload_file_delete
        self.rclone_path = rclone_path
        self.remote_dir = remote_dir
        self.upload_adapter = upload_adapter
        self.dir_cache: dict = {}  # for remote mkdir
        self.total_upload_success_file_count = 0
        self.aligo = None

    def pre_run(self):
        """pre run init aligo"""
        if self.enable_upload_file and self.upload_adapter == "aligo":
            CloudDrive.init_upload_adapter(self)


class CloudDrive:
    """rclone support"""

    @staticmethod
    def init_upload_adapter(drive_config: CloudDriveConfig):
        """Initialize the upload adapter."""
        if drive_config.upload_adapter == "aligo":
            Aligo = importlib.import_module("aligo").Aligo
            drive_config.aligo = Aligo()

    @staticmethod
    def rclone_copyto_args(
        rclone_path: str, file_path: str, remote_file_path: str
    ) -> list:
        """Build rclone copyto argv.

        Arguments are passed as a list to ``create_subprocess_exec``. Never
        interpolate channel titles or Telegram file names into a shell command:
        ``$()``, backticks and ``%VAR%`` are expanded even inside double quotes.
        """
        return [
            str(rclone_path),
            "copyto",
            str(file_path),
            str(remote_file_path),
            "--no-traverse",
            "--progress",
            # -v is what makes rclone print the per-file verdict
            # ("Copied (new)" / "Unchanged skipping") we parse below.
            "-v",
        ]

    @staticmethod
    def remote_dir_for(
        drive_config: CloudDriveConfig, save_path: str, local_file_path: str
    ) -> str:
        """Map a local directory under ``save_path`` to the remote directory.

        ``str.replace`` alone produced paths that still contained the local
        absolute path when ``save_path`` had a trailing slash or a different
        separator, so uploads landed in a new remote tree and looked "new" next
        to identical files that were already there.
        """
        local_dir = os.path.dirname(local_file_path)
        relative_dir = ""
        candidate = None
        try:
            candidate = os.path.relpath(local_dir, save_path)
        except (TypeError, ValueError):
            candidate = None
        if candidate is not None and not candidate.startswith(".."):
            relative_dir = "" if candidate == "." else candidate
        else:
            relative_dir = local_dir.replace(save_path, "").strip("/\\")

        remote_dir = (
            drive_config.remote_dir + "/" + relative_dir.replace("\\", "/") + "/"
        )
        # Collapse duplicate slashes while keeping the scheme part intact.
        remote_dir = re.sub(r"(?<!:)/{2,}", "/", remote_dir)
        return remote_dir

    @staticmethod
    def rclone_mkdir_args(rclone_path: str, remote_dir: str) -> list:
        """Build rclone mkdir argv."""
        return [str(rclone_path), "mkdir", str(remote_dir)]

    @staticmethod
    async def rclone_mkdir(drive_config: CloudDriveConfig, remote_dir: str) -> bool:
        """mkdir in remote without a shell."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *CloudDrive.rclone_mkdir_args(drive_config.rclone_path, remote_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            output, _ = await proc.communicate()
            if proc.returncode != 0:
                logger.warning(
                    f"rclone mkdir failed, exit code {proc.returncode}: {remote_dir}\n"
                    f"{output.decode(errors='replace')[-400:]}"
                )
                return False
        except Exception as e:
            logger.warning(f"rclone mkdir {remote_dir} failed: {e}")
            return False
        return True

    @staticmethod
    def aligo_mkdir(drive_config: CloudDriveConfig, remote_dir: str):
        """mkdir in remote by aligo"""
        if drive_config.aligo and not drive_config.aligo.get_folder_by_path(remote_dir):
            drive_config.aligo.create_folder(name=remote_dir, check_name_mode="refuse")

    @staticmethod
    def zip_file(local_file_path: str) -> str:
        """
        Zip local file
        """

        file_path_without_extension = os.path.splitext(local_file_path)[0]
        zip_file_name = file_path_without_extension + ".zip"

        with ZipFile(zip_file_name, "w") as zip_writer:
            zip_writer.write(local_file_path)

        return zip_file_name

    @staticmethod
    def rclone_stat_args(rclone_path: str, remote_file_path: str) -> list:
        """Build ``rclone lsjson --stat`` argv for a single remote object."""
        return [str(rclone_path), "lsjson", "--stat", str(remote_file_path)]

    @staticmethod
    async def rclone_remote_stat(
        drive_config: CloudDriveConfig, remote_file_path: str
    ) -> Optional[dict]:
        """Return the remote object's metadata, or ``None`` when unknown/missing.

        ``rclone lsjson --stat`` prints one JSON object with at least ``Size``
        and ``ModTime`` for most backends. A missing object (or an old rclone
        without ``--stat``) yields ``None`` so the caller can fall back to
        letting rclone decide during the copy.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                *CloudDrive.rclone_stat_args(
                    drive_config.rclone_path, remote_file_path
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return None
            payload = json.loads(stdout.decode(errors="replace") or "{}")
            if isinstance(payload, dict) and payload:
                return payload
            if isinstance(payload, list) and payload:
                return payload[0]
        except Exception as e:  # pylint: disable = W0703
            logger.debug(f"remote stat failed for {remote_file_path}: {e}")
        return None

    @staticmethod
    def _parse_remote_time(value) -> Optional[float]:
        """Parse an rclone ModTime (RFC 3339 with optional offset) to epoch."""
        if not isinstance(value, str):
            return None
        match = _REMOTE_TIME_RE.search(value)
        if not match:
            return None
        if int(match.group(1)) < 2000:
            # Backends without modtime support report the zero time.
            return None
        try:
            base = datetime(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
                int(match.group(4)),
                int(match.group(5)),
                int(match.group(6)),
            )
        except ValueError:
            return None
        zone = match.group(7)
        if zone == "Z":
            base = base.replace(tzinfo=timezone.utc)
        elif zone:
            sign = 1 if zone[0] == "+" else -1
            hours, minutes = zone[1:].split(":")
            base = base.replace(
                tzinfo=timezone(
                    sign * timedelta(hours=int(hours), minutes=int(minutes))
                )
            )
        return base.timestamp()

    @staticmethod
    def _delete_uploaded_local(
        drive_config: CloudDriveConfig, local_file_path: str
    ) -> None:
        """Delete the local copy after a successful upload, when configured."""
        if not drive_config.after_upload_file_delete:
            return
        try:
            os.remove(local_file_path)
        except FileNotFoundError:
            return
        except OSError as remove_err:
            logger.warning(
                f"remove uploaded file {local_file_path} failed: {remove_err}"
            )

    # pylint: disable = R0914
    @staticmethod
    async def rclone_upload_file(
        drive_config: CloudDriveConfig,
        save_path: str,
        local_file_path: str,
        progress_callback: Callable = None,
        progress_args: tuple = (),
    ) -> Optional[str]:
        """Upload one file with rclone and report whether it really was new.

        Behaviour worth knowing:

          * The remote object is inspected first (``rclone lsjson --stat``). If it
            already exists with the same size the upload is skipped and reported
            as ``UPLOAD_EXISTS``. Without this check rclone compared the freshly
            downloaded file's mtime with the remote one, decided they differed and
            re-uploaded every file, so a drive that deduplicates by hash kept the
            old object (and its old date) while rclone still said "Copied (new)".
          * ``copyto`` targets the exact remote path instead of ``copy`` into a
            directory, so the uploaded name is unambiguous.
          * argv goes through ``create_subprocess_exec``: channel titles and
            Telegram file names are attacker-controlled and previously allowed
            shell injection.
          * ``--size-only`` is gone; the explicit size check above replaces it
            without deleting local files when the remote copy differs.
          * After an upload the remote is inspected again. If the backend kept an
            old object (server-side deduplication), the result is reported as
            ``UPLOAD_EXISTS`` instead of pretending a new file was written.
        """
        upload_state: Optional[str] = None
        zip_file_path: str = ""
        copied_new = False
        skipped_existing = False
        verdict = ""
        try:
            remote_dir = CloudDrive.remote_dir_for(
                drive_config, save_path, local_file_path
            )

            if not drive_config.dir_cache.get(remote_dir):
                if await CloudDrive.rclone_mkdir(drive_config, remote_dir):
                    drive_config.dir_cache[remote_dir] = True

            file_path = local_file_path
            if drive_config.before_upload_file_zip:
                zip_file_path = CloudDrive.zip_file(local_file_path)
                file_path = zip_file_path

            try:
                local_size = os.path.getsize(file_path)
            except OSError:
                # The file vanished or the path is virtual; fall back to letting
                # rclone decide instead of failing the whole upload.
                local_size = None
            remote_file_path = (
                f"{remote_dir.rstrip('/')}/{os.path.basename(file_path)}"
            )

            existing = await CloudDrive.rclone_remote_stat(
                drive_config, remote_file_path
            )
            existing_size = None
            if existing:
                try:
                    existing_size = int(existing.get("Size"))
                except (TypeError, ValueError):
                    existing_size = None

            if (
                existing_size is not None
                and local_size is not None
                and existing_size == local_size
            ):
                # Same name, same size: keep the remote object (and its date)
                # untouched instead of re-uploading it on every run.
                logger.info(
                    f"remote already has {remote_file_path} "
                    f"({local_size} bytes), upload skipped"
                )
                drive_config.total_upload_success_file_count += 1
                CloudDrive._delete_uploaded_local(drive_config, local_file_path)
                return UPLOAD_EXISTS

            if existing_size is not None and local_size is not None:
                logger.info(
                    f"remote file exists with a different size "
                    f"({existing_size} != {local_size}), replacing: "
                    f"{remote_file_path}"
                )

            upload_started = time.time()
            proc = await asyncio.create_subprocess_exec(
                *CloudDrive.rclone_copyto_args(
                    drive_config.rclone_path, file_path, remote_file_path
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            tail: list = []
            if proc.stdout:
                async for output in proc.stdout:
                    s = output.decode(errors="replace")
                    tail.append(s)
                    if len(tail) > 100:
                        tail.pop(0)
                    lower = s.lower()
                    if "copied (new)" in lower or "copied (replaced" in lower:
                        copied_new = True
                        verdict = s.strip()
                    elif "copied (server-side copy)" in lower:
                        copied_new = True
                        verdict = s.strip()
                    elif "unchanged skipping" in lower:
                        skipped_existing = True
                        verdict = s.strip()
                    transferred_match = re.search(
                        r"Transferred: (.*?) / (.*?), (.*?)%, (.*?/s)?, ETA (.*?)$", s
                    )
                    if transferred_match and progress_callback:
                        func = functools.partial(
                            progress_callback,
                            transferred_match.group(1),
                            transferred_match.group(2),
                            transferred_match.group(3),
                            transferred_match.group(4),
                            transferred_match.group(5),
                            *progress_args,
                        )
                        if inspect.iscoroutinefunction(progress_callback):
                            await func()

            retcode = await proc.wait()

            if retcode == 0:
                if skipped_existing and not copied_new:
                    upload_state = UPLOAD_EXISTS
                else:
                    upload_state = UPLOAD_NEW

                if (
                    upload_state == UPLOAD_NEW
                    and existing_size is None
                    and local_size is not None
                ):
                    after = await CloudDrive.rclone_remote_stat(
                        drive_config, remote_file_path
                    )
                    remote_time = (
                        CloudDrive._parse_remote_time(after.get("ModTime"))
                        if after
                        else None
                    )
                    if (
                        remote_time is not None
                        and upload_started - remote_time > _STALE_REMOTE_SECONDS
                    ):
                        logger.warning(
                            f"remote kept the older object at {remote_file_path} "
                            f"(modtime {after.get('ModTime')}); the drive "
                            f"deduplicated the upload, counting it as already existing"
                        )
                        upload_state = UPLOAD_EXISTS

                drive_config.total_upload_success_file_count += 1
                CloudDrive._delete_uploaded_local(drive_config, local_file_path)
                suffix = f" (rclone: {verdict})" if verdict else ""
                if upload_state == UPLOAD_EXISTS:
                    logger.info(
                        f"remote already has {remote_file_path}, upload skipped"
                        f"{suffix}"
                    )
                else:
                    logger.info(
                        f"upload new file {local_file_path} -> "
                        f"{remote_file_path} success{suffix}"
                    )
            else:
                logger.error(
                    f"rclone upload failed, exit code {retcode}: "
                    f"{local_file_path}\n{''.join(tail)[-800:]}"
                )
        except Exception as e:
            logger.error(f"{e.__class__} {e}")
            return None
        finally:
            # The zip is only a temporary transport artifact; never leave it in
            # downloads/, even when the upload failed.
            if zip_file_path and os.path.exists(zip_file_path):
                try:
                    os.remove(zip_file_path)
                except OSError as zip_err:
                    logger.warning(f"remove zip {zip_file_path} failed: {zip_err}")

        return upload_state

    @staticmethod
    def aligo_upload_file(
        drive_config: CloudDriveConfig, save_path: str, local_file_path: str
    ) -> Optional[str]:
        """aliyun upload file"""
        if not drive_config.aligo:
            logger.warning("please config aligo! see README.md")
            return None

        try:
            remote_dir = CloudDrive.remote_dir_for(
                drive_config, save_path, local_file_path
            )

            if not drive_config.dir_cache.get(remote_dir):
                CloudDrive.aligo_mkdir(drive_config, remote_dir)
                aligo_dir = drive_config.aligo.get_folder_by_path(remote_dir)
                if aligo_dir:
                    drive_config.dir_cache[remote_dir] = aligo_dir.file_id

            zip_file_path: str = ""
            file_paths = []
            if drive_config.before_upload_file_zip:
                zip_file_path = CloudDrive.zip_file(local_file_path)
                file_paths.append(zip_file_path)
            else:
                file_paths.append(local_file_path)

            res = drive_config.aligo.upload_files(
                file_paths=file_paths,
                parent_file_id=drive_config.dir_cache[remote_dir],
                check_name_mode="refuse",
            )

            if len(res) > 0:
                drive_config.total_upload_success_file_count += len(res)
                if drive_config.after_upload_file_delete:
                    os.remove(local_file_path)

                if drive_config.before_upload_file_zip:
                    os.remove(zip_file_path)

                return UPLOAD_NEW

        except Exception as e:
            logger.error(f"{e.__class__} {e}")
            return None

        return None

    @staticmethod
    async def upload_file(
        drive_config: CloudDriveConfig, save_path: str, local_file_path: str
    ) -> Optional[str]:
        """Upload file

        Parameters
        ----------
        drive_config: CloudDriveConfig
            see @CloudDriveConfig

        save_path: str
            Local file save path config

        local_file_path: str
            Local file path

        Returns
        -------
        Optional[str]
            ``UPLOAD_NEW`` when bytes were transferred, ``UPLOAD_EXISTS`` when
            the remote already had the file, ``None`` on failure.
        """
        if not drive_config.enable_upload_file:
            return None

        ret: Optional[str] = None
        if drive_config.upload_adapter == "rclone":
            ret = await CloudDrive.rclone_upload_file(
                drive_config, save_path, local_file_path
            )
        elif drive_config.upload_adapter == "aligo":
            ret = CloudDrive.aligo_upload_file(drive_config, save_path, local_file_path)

        return ret
