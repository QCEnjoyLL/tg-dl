"""provide upload cloud drive"""
import asyncio
import functools
import importlib
import inspect
import os
import re
from asyncio import subprocess
from typing import Callable, Optional
from zipfile import ZipFile

from loguru import logger

from utils import platform

#: Upload results. ``UPLOAD_NEW`` means bytes were actually transferred,
#: ``UPLOAD_EXISTS`` means the remote already held an identical file and rclone
#: skipped it (still a success, but the operator deserves to know which one).
UPLOAD_NEW = "uploaded"
UPLOAD_EXISTS = "exists"


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

    # pylint: disable = R0914
    @staticmethod
    async def rclone_upload_file(
        drive_config: CloudDriveConfig,
        save_path: str,
        local_file_path: str,
        progress_callback: Callable = None,
        progress_args: tuple = (),
    ) -> bool:
        """Upload one file with rclone and delete the local copy on success.

        Differences from upstream:
          * ``copyto`` targets the exact remote path instead of ``copy`` into a
            directory, so the uploaded name is unambiguous.
          * ``--ignore-existing`` is gone. Upstream skipped the upload whenever a
            same-named file existed remotely, never printed the "Transferred ...
            100% ... 1 / 1" line it greps for, and therefore never deleted the
            local file. That is what filled users' disks up.
          * success is decided by the rclone exit code, not by scraping the
            progress output, which small/fast uploads never emit.
          * argv is passed to ``create_subprocess_exec`` instead of being built
            into a ``shell=True`` command. Channel titles and Telegram file names
            are attacker-controlled and previously allowed shell injection.
          * ``--size-only`` was removed: a same-size-but-different remote object
            made rclone skip the transfer, return 0, and the local copy was then
            deleted.
        """
        upload_state: Optional[str] = None
        zip_file_path: str = ""
        copied_new = False
        skipped_existing = False
        try:
            remote_dir = (
                drive_config.remote_dir
                + "/"
                + os.path.dirname(local_file_path).replace(save_path, "")
                + "/"
            ).replace("\\", "/")

            if not drive_config.dir_cache.get(remote_dir):
                if await CloudDrive.rclone_mkdir(drive_config, remote_dir):
                    drive_config.dir_cache[remote_dir] = True

            file_path = local_file_path
            if drive_config.before_upload_file_zip:
                zip_file_path = CloudDrive.zip_file(local_file_path)
                file_path = zip_file_path

            remote_file_path = f"{remote_dir.rstrip('/')}/{os.path.basename(file_path)}"
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
                    elif "copied (server-side copy)" in lower:
                        copied_new = True
                    elif "unchanged skipping" in lower:
                        skipped_existing = True
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
                    logger.info(
                        f"remote already has {local_file_path}, upload skipped "
                        f"(this is a success)"
                    )
                else:
                    upload_state = UPLOAD_NEW
                    logger.info(f"upload new file {local_file_path} success")
                drive_config.total_upload_success_file_count += 1
                if drive_config.after_upload_file_delete:
                    try:
                        os.remove(local_file_path)
                    except OSError as remove_err:
                        logger.warning(
                            f"remove uploaded file {local_file_path} failed: {remove_err}"
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
            remote_dir = (
                drive_config.remote_dir
                + "/"
                + os.path.dirname(local_file_path).replace(save_path, "")
                + "/"
            ).replace("\\", "/")

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
