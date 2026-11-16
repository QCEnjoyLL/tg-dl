"""Application module"""

import asyncio
import functools
import io
import os
import shutil
import time
from asyncio import Lock
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable, List, Optional, Tuple, Union

from loguru import logger
from ruamel import yaml

from module.cloud_drive import CloudDrive, CloudDriveConfig
from module.config_form import MEDIA_TYPE_OPTIONS, parse_format_list
from module.fast_download import set_chunk_concurrency
from module.filter import Filter
from module.language import Language, set_language
from utils.atomic_write import write_text_atomic
from utils.format import replace_date_time, validate_title
from utils.meta_data import MetaData

_yaml = yaml.YAML()
# pylint: disable = R0902


class DownloadStatus(Enum):
    """Download status"""

    SkipDownload = 1
    SuccessDownload = 2
    FailedDownload = 3
    Downloading = 4


class ForwardStatus(Enum):
    """Forward status"""

    SkipForward = 1
    SuccessForward = 2
    FailedForward = 3
    Forwarding = 4
    StopForward = 5
    CacheForward = 6


class UploadStatus(Enum):
    """Upload status"""

    SkipUpload = 1
    SuccessUpload = 2
    FailedUpload = 3
    Uploading = 4


class TaskType(Enum):
    """Task Type"""

    Download = 1
    Forward = 2
    ListenForward = 3


class QueryHandler(Enum):
    """Query handler"""

    StopDownload = 1
    StopForward = 2
    StopListenForward = 3


@dataclass
class UploadProgressStat:
    """Upload task"""

    file_name: str
    total_size: int
    upload_size: int
    start_time: float
    last_stat_time: float
    upload_speed: float


@dataclass
class CloudDriveUploadStat:
    """Cloud drive upload task"""

    file_name: str
    transferred: str
    total: str
    percentage: str
    speed: str
    eta: str


class QueryHandlerStr:
    """Query handler"""

    _strMap = {
        QueryHandler.StopDownload.value: "stop_download",
        QueryHandler.StopForward.value: "stop_forward",
        QueryHandler.StopListenForward.value: "stop_listen_forward",
    }

    @staticmethod
    def get_str(value):
        """
        Get the string value associated with the given value.

        Parameters:
            value (any): The value for which to retrieve the string value.

        Returns:
            str: The string value associated with the given value.
        """
        return QueryHandlerStr._strMap[value]


class TaskNode:
    """Task node"""

    # pylint: disable = R0913
    def __init__(
        self,
        chat_id: Union[int, str],
        from_user_id: Union[int, str] = None,
        reply_message_id: int = 0,
        replay_message: str = None,
        upload_telegram_chat_id: Union[int, str] = None,
        has_protected_content: bool = False,
        download_filter: str = None,
        limit: int = 0,
        start_offset_id: int = 0,
        end_offset_id: int = 0,
        bot=None,
        task_type: TaskType = TaskType.Download,
        task_id: int = 0,
        topic_id: int = 0,
    ):
        self.chat_id = chat_id
        self.from_user_id = from_user_id
        self.upload_telegram_chat_id = upload_telegram_chat_id
        self.reply_message_id = reply_message_id
        self.reply_message = replay_message
        self.has_protected_content = has_protected_content
        self.download_filter = download_filter
        self.limit = limit
        self.start_offset_id = start_offset_id
        self.end_offset_id = end_offset_id
        self.bot = bot
        self.task_id = task_id
        self.task_type = task_type
        self.total_task = 0
        self.total_download_task = 0
        self.failed_download_task = 0
        self.success_download_task = 0
        self.skip_download_task = 0
        self.last_reply_time = time.time()
        self.last_edit_msg: str = ""
        self.total_download_byte = 0
        self.forward_msg_detail_str: str = ""
        self.upload_user = None
        self.total_forward_task: int = 0
        self.success_forward_task: int = 0
        self.failed_forward_task: int = 0
        self.skip_forward_task: int = 0
        self.is_running: bool = False
        #: True while the chat history is still being scanned for this task.
        self.is_scanning: bool = False
        self.client = None
        self.upload_success_count: int = 0
        self.upload_skipped_count: int = 0
        self.is_stop_transmission = False
        self.media_group_ids: dict = {}
        self.media_group_ids_lock: Lock = Lock()
        self.download_status: dict = {}
        self.upload_status: dict = {}
        self.upload_stat_dict: dict = {}
        self.topic_id = topic_id
        self.reply_to_message = None
        self.cloud_drive_upload_stat_dict: dict = {}
        #: Per-message failure reason, filled by download_media and consumed by
        #: download_task when it records the failure for the Web UI.
        self.last_error: dict = {}

    def skip_msg_id(self, msg_id: int):
        """Skip if message id out of range"""
        if self.start_offset_id and msg_id < self.start_offset_id:
            return True

        if self.end_offset_id and msg_id > self.end_offset_id:
            return True

        return False

    def is_finish(self):
        """If is finish"""
        return self.is_stop_transmission or (
            self.is_running
            and not self.is_scanning
            and self.task_type != TaskType.ListenForward
            and self.total_task == self.total_download_task
        )

    def stop_transmission(self):
        """Stop task"""
        self.is_stop_transmission = True

    def stat(self, status: DownloadStatus):
        """
        Updates the download status of the task.

        Args:
            status (DownloadStatus): The status of the download task.

        Returns:
            None
        """
        # Downloading is not a terminal state. Counting it as a failure made the
        # bot card drift and hid real failures behind phantom failed tasks.
        if status is DownloadStatus.Downloading:
            return

        self.total_download_task += 1
        if status is DownloadStatus.SuccessDownload:
            self.success_download_task += 1
        elif status is DownloadStatus.SkipDownload:
            self.skip_download_task += 1
        else:
            self.failed_download_task += 1

    def stat_forward(self, status: ForwardStatus, count: int = 1):
        """Stat upload"""
        self.total_forward_task += count
        if status is ForwardStatus.SuccessForward:
            self.success_forward_task += count
        elif status is ForwardStatus.SkipForward:
            self.skip_forward_task += count
        else:
            self.failed_forward_task += count

    def can_reply(self):
        """
        Checks if the bot can reply to a message
            based on the time elapsed since the last reply.

        Returns:
            True if the time elapsed since
                the last reply is greater than 1 second, False otherwise.
        """
        cur_time = time.time()
        # Telegram allows roughly one edit per second per message and starts
        # answering FloodWait quickly. Three seconds keeps the card alive for
        # long batches instead of being throttled into silence.
        if cur_time - self.last_reply_time > 3.0:
            self.last_reply_time = cur_time
            return True

        return False


class LimitCall:
    """Limit call"""

    def __init__(
        self,
        max_limit_call_times: int = 0,
        limit_call_times: int = 0,
        last_call_time: float = 0,
    ):
        """
        Initializes the object with the given parameters.

        Args:
            max_limit_call_times (int): The maximum limit of call times allowed.
            limit_call_times (int): The current limit of call times.
            last_call_time (int): The time of the last call.

        Returns:
            None
        """
        self.max_limit_call_times = max_limit_call_times
        self.limit_call_times = limit_call_times
        self.last_call_time = last_call_time

    async def wait(self, node: TaskNode):
        """
        Wait for a certain period of time before continuing execution.

        This function does not take any parameters.

        This function does not return anything.
        """
        while True:
            now = time.time()
            time_span = now - self.last_call_time
            if node.is_stop_transmission:
                break

            if time_span > 60:
                self.limit_call_times = 0
                self.last_call_time = now

            if self.limit_call_times + 1 <= self.max_limit_call_times:
                self.limit_call_times += 1
                break

            # logger.debug("Waiting for 10 seconds...")
            await asyncio.sleep(1)


class ChatDownloadConfig:
    """Chat Message Download Status"""

    def __init__(self):
        self.ids_to_retry_dict: dict = {}

        # need storage
        self.download_filter: str = None
        self.ids_to_retry: list = []
        self.last_read_message_id = 0
        self.total_task: int = 0
        self.finish_task: int = 0
        self.need_check: bool = False
        self.upload_telegram_chat_id: Union[int, str] = None
        self.node: TaskNode = TaskNode(0)


def get_config(config, key, default=None, val_type=str, verbose=True):
    """
    Retrieves a configuration value from the given `config` dictionary
    based on the specified `key`.

    Args:
        config (dict): A dictionary containing the configuration values.
        key (str): The key of the configuration value to retrieve.
        default (Any, optional): The default value to be returned
            if the `key` is not found.
        val_type (type, optional): The data type of the configuration value.
        verbose (bool, optional): A flag indicating whether to print
            a warning message if the `key` is not found.

    Returns:
        The configuration value associated with the specified `key`,
         converted to the specified `type`. If the `key` is not found,
         the `default` value is returned.
    """
    val = config.get(key, default)
    if isinstance(val, val_type):
        return val

    if verbose:
        logger.warning(f"{key} is not {val_type.__name__}")

    return default


class Application:
    """Application load config and update config."""

    def __init__(
        self,
        config_file: str,
        app_data_file: str,
        application_name: str = "UndefineApp",
    ):
        """
        Init and update telegram media downloader config

        Parameters
        ----------
        config_file: str
            Config file name

        app_data_file: str
            App data file

        application_name: str
            Application Name

        """
        self.config_file: str = config_file
        self.app_data_file: str = app_data_file
        self.application_name: str = application_name
        self.download_filter = Filter()
        self.is_running = True

        self.total_download_task = 0

        self.chat_download_config: dict = {}

        self.save_path = os.path.join(os.path.abspath("."), "downloads")
        self.temp_save_path = os.path.join(os.path.abspath("."), "temp")
        self.api_id: str = ""
        self.api_hash: str = ""
        self.bot_token: str = ""
        self._chat_id: str = ""
        self.media_types: List[str] = []
        self.file_formats: dict = {}
        self.proxy: dict = {}
        self.restart_program = False
        self.config: dict = {}
        self.app_data: dict = {}
        self.file_path_prefix: List[str] = ["chat_title", "media_datetime"]
        self.file_name_prefix: List[str] = ["message_id", "file_name"]
        self.file_name_prefix_split: str = " - "
        self.log_file_path = os.path.join(os.path.abspath("."), "log")
        self.session_file_path = os.path.join(os.path.abspath("."), "sessions")
        self.cloud_drive_config = CloudDriveConfig()
        self.hide_file_name = False
        self.caption_name_dict: dict = {}
        self.caption_entities_dict: dict = {}
        self.max_concurrent_transmissions: int = 1
        self.web_host: str = "0.0.0.0"
        self.web_port: int = 5000
        self.max_download_task: int = 5
        #: Ranges one file is split into concurrently (1 = sequential download).
        self.max_concurrent_chunks: int = 1
        self.language = Language.EN
        self.after_upload_telegram_delete: bool = True
        self.web_login_secret: str = ""
        self.debug_web: bool = False
        self.log_level: str = "INFO"
        self.start_timeout: int = 60
        self.allowed_user_ids: yaml.comments.CommentedSeq = yaml.comments.CommentedSeq(
            []
        )
        self.date_format: str = "%Y_%m"
        self.drop_no_audio_video: bool = False
        self.enable_download_txt: bool = False
        self.filter_advertisement_list: yaml.comments.CommentedSeq = (
            yaml.comments.CommentedSeq([])
        )
        self.replace_advertisement_list: yaml.comments.CommentedSeq = (
            yaml.comments.CommentedSeq([])
        )
        self.group_add_advertisement: dict = {}
        self.forward_limit_call = LimitCall(max_limit_call_times=33)

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        self.executor = ThreadPoolExecutor(
            min(32, (os.cpu_count() or 0) + 4), thread_name_prefix="multi_task"
        )

    # pylint: disable = R0915
    def assign_config(self, _config: dict) -> bool:
        """assign config from str.

        Parameters
        ----------
        _config: dict
            application config dict

        Returns
        -------
        bool
        """
        # pylint: disable = R0912
        # TODO: judge the storage if enough,and provide more path
        if _config.get("save_path") is not None:
            self.save_path = _config["save_path"]

        self.api_id = _config["api_id"]
        self.api_hash = _config["api_hash"]
        self.bot_token = _config.get("bot_token", "")

        self.media_types = self._normalize_media_types(_config.get("media_types"))
        self.file_formats = self._normalize_file_formats(_config.get("file_formats"))

        self.hide_file_name = _config.get("hide_file_name", False)

        # option
        if _config.get("proxy"):
            self.proxy = _config["proxy"]
        if _config.get("restart_program"):
            self.restart_program = _config["restart_program"]
        if _config.get("file_path_prefix"):
            self.file_path_prefix = _config["file_path_prefix"]
        if _config.get("file_name_prefix"):
            self.file_name_prefix = _config["file_name_prefix"]

        if _config.get("upload_drive"):
            upload_drive_config = _config["upload_drive"]
            if upload_drive_config.get("enable_upload_file"):
                self.cloud_drive_config.enable_upload_file = upload_drive_config[
                    "enable_upload_file"
                ]

            if upload_drive_config.get("rclone_path"):
                self.cloud_drive_config.rclone_path = upload_drive_config["rclone_path"]

            if upload_drive_config.get("remote_dir"):
                self.cloud_drive_config.remote_dir = upload_drive_config["remote_dir"]

            if upload_drive_config.get("before_upload_file_zip"):
                self.cloud_drive_config.before_upload_file_zip = upload_drive_config[
                    "before_upload_file_zip"
                ]

            if upload_drive_config.get("after_upload_file_delete"):
                self.cloud_drive_config.after_upload_file_delete = upload_drive_config[
                    "after_upload_file_delete"
                ]

            if upload_drive_config.get("upload_adapter"):
                self.cloud_drive_config.upload_adapter = upload_drive_config[
                    "upload_adapter"
                ]

        self.file_name_prefix_split = _config.get(
            "file_name_prefix_split", self.file_name_prefix_split
        )
        self.web_host = _config.get("web_host", self.web_host)
        self.web_port = _config.get("web_port", self.web_port)

        # TODO: add check if expression exist syntax error

        self.max_download_task = _config.get(
            "max_download_task", self.max_download_task
        )

        self.max_concurrent_transmissions = self.max_download_task * 5

        self.max_concurrent_transmissions = _config.get(
            "max_concurrent_transmissions", self.max_concurrent_transmissions
        )

        self.max_concurrent_chunks = set_chunk_concurrency(
            get_config(
                _config, "max_concurrent_chunks", self.max_concurrent_chunks, int
            )
        )

        language = _config.get("language", "EN")

        try:
            self.language = Language[language.upper()]
        except KeyError:
            pass

        self.after_upload_telegram_delete = _config.get(
            "after_upload_telegram_delete", self.after_upload_telegram_delete
        )

        self.web_login_secret = str(
            _config.get("web_login_secret", self.web_login_secret)
        )
        self.debug_web = _config.get("debug_web", self.debug_web)
        self.log_level = _config.get("log_level", self.log_level)

        self.start_timeout = get_config(
            _config, "start_timeout", self.start_timeout, int
        )

        self.allowed_user_ids = get_config(
            _config,
            "allowed_user_ids",
            self.allowed_user_ids,
            yaml.comments.CommentedSeq,
        )

        self.date_format = get_config(
            _config,
            "date_format",
            self.date_format,
            str,
        )

        self.drop_no_audio_video = get_config(
            _config, "drop_no_audio_video", self.drop_no_audio_video, bool
        )

        self.enable_download_txt = get_config(
            _config, "enable_download_txt", self.enable_download_txt, bool
        )

        self.filter_advertisement_list = get_config(
            _config,
            "filter_advertisement_list",
            self.filter_advertisement_list,
            yaml.comments.CommentedSeq,
        )

        self.replace_advertisement_list = get_config(
            _config,
            "replace_advertisement_list",
            self.replace_advertisement_list,
            yaml.comments.CommentedSeq,
        )

        if _config.get("group_add_advertisement"):
            self.group_add_advertisement = _config["group_add_advertisement"]
        try:
            date = datetime(2023, 10, 31)
            date.strftime(self.date_format)
        except Exception as e:
            logger.warning(f"config date format error: {e}")
            self.date_format = "%Y_%m"

        forward_limit = _config.get("forward_limit", None)
        if forward_limit:
            try:
                forward_limit = int(forward_limit)
                self.forward_limit_call.max_limit_call_times = forward_limit
            except ValueError:
                pass

        if _config.get("chat"):
            chat = _config["chat"]
            for item in chat:
                if not isinstance(item, dict):
                    continue
                chat_id = item.get("chat_id")
                # A blank/None chat_id used to reach the download loop and blow
                # up with "NoneType < int", silently preventing every download.
                if chat_id is None or str(chat_id).strip().lower() in (
                    "",
                    "none",
                    "null",
                    "~",
                ):
                    logger.warning(
                        "config.yaml 中有一个 chat 缺少 chat_id，已跳过；"
                        "请在 Web UI「下载的频道 / 群组」里重新填写频道 ID。"
                    )
                    continue
                last_read = item.get("last_read_message_id") or 0
                self.chat_download_config[chat_id] = ChatDownloadConfig()
                self.chat_download_config[chat_id].last_read_message_id = int(
                    last_read
                )
                self.chat_download_config[chat_id].download_filter = (
                    item.get("download_filter") or ""
                )
                self.chat_download_config[chat_id].upload_telegram_chat_id = item.get(
                    "upload_telegram_chat_id", None
                )
        elif _config.get("chat_id"):
            # Compatible with lower versions
            self._chat_id = _config["chat_id"]

            self.chat_download_config[self._chat_id] = ChatDownloadConfig()

            if _config.get("ids_to_retry"):
                self.chat_download_config[self._chat_id].ids_to_retry = _config[
                    "ids_to_retry"
                ]
                for it in self.chat_download_config[self._chat_id].ids_to_retry:
                    self.chat_download_config[self._chat_id].ids_to_retry_dict[
                        it
                    ] = True

            self.chat_download_config[self._chat_id].last_read_message_id = _config[
                "last_read_message_id"
            ]
            download_filter_dict = _config.get("download_filter", None)

            self.config["chat"] = [
                {
                    "chat_id": self._chat_id,
                    "last_read_message_id": self.chat_download_config[
                        self._chat_id
                    ].last_read_message_id,
                }
            ]

            if download_filter_dict and self._chat_id in download_filter_dict:
                self.chat_download_config[
                    self._chat_id
                ].download_filter = download_filter_dict[self._chat_id]
                self.config["chat"][0]["download_filter"] = download_filter_dict[
                    self._chat_id
                ]

        # pylint: disable = R1733
        for key, value in self.chat_download_config.items():
            self.chat_download_config[key].download_filter = replace_date_time(
                value.download_filter
            )

        return True

    def assign_app_data(self, app_data: dict) -> bool:
        """Assign config from str.

        Parameters
        ----------
        app_data: dict
            application data dict

        Returns
        -------
        bool
        """
        if app_data.get("ids_to_retry"):
            if self._chat_id:
                self.chat_download_config[self._chat_id].ids_to_retry = app_data[
                    "ids_to_retry"
                ]
                for it in self.chat_download_config[self._chat_id].ids_to_retry:
                    self.chat_download_config[self._chat_id].ids_to_retry_dict[
                        it
                    ] = True
                self.app_data.pop("ids_to_retry")
        else:
            if app_data.get("chat"):
                chats = app_data["chat"]
                for chat in chats:
                    if (
                        "chat_id" in chat
                        and chat["chat_id"] in self.chat_download_config
                    ):
                        chat_id = chat["chat_id"]
                        self.chat_download_config[chat_id].ids_to_retry = chat.get(
                            "ids_to_retry", []
                        )
                        for it in self.chat_download_config[chat_id].ids_to_retry:
                            self.chat_download_config[chat_id].ids_to_retry_dict[
                                it
                            ] = True
        return True

    async def upload_file(
        self,
        local_file_path: str,
        progress_callback: Callable = None,
        progress_args: tuple = (),
    ) -> Optional[str]:
        """Upload file and report whether it was new or already on the remote.

        Returns ``module.cloud_drive.UPLOAD_NEW`` when bytes were transferred,
        ``UPLOAD_EXISTS`` when the remote already had an identical file, and
        ``None`` on failure.
        """

        if not self.cloud_drive_config.enable_upload_file:
            return None

        ret: Optional[str] = None
        if self.cloud_drive_config.upload_adapter == "rclone":
            ret = await CloudDrive.rclone_upload_file(
                self.cloud_drive_config,
                self.save_path,
                local_file_path,
                progress_callback,
                progress_args,
            )
        elif self.cloud_drive_config.upload_adapter == "aligo":
            # partial(): calling the sync adapter inside run_in_executor's
            # argument list used to run it on the event loop and then hand the
            # bool result to the executor as a callable.
            ret = await self.loop.run_in_executor(
                self.executor,
                functools.partial(
                    CloudDrive.aligo_upload_file,
                    self.cloud_drive_config,
                    self.save_path,
                    local_file_path,
                ),
            )

        return ret

    def get_file_save_path(
        self, media_type: str, chat_title: str, media_datetime: str
    ) -> str:
        """Get file save path prefix.

        Parameters
        ----------
        media_type: str
            see config.yaml media_types

        chat_title: str
            see channel or group title

        media_datetime: str
            media datetime

        Returns
        -------
        str
            file save path prefix
        """

        res: str = self.save_path
        for prefix in self.file_path_prefix:
            if prefix == "chat_title":
                res = os.path.join(res, chat_title)
            elif prefix == "media_datetime":
                res = os.path.join(res, media_datetime)
            elif prefix == "media_type":
                res = os.path.join(res, media_type)
        return res

    def get_file_name(
        self, message_id: int, file_name: Optional[str], caption: Optional[str]
    ) -> str:
        """Get file save path prefix.

        Parameters
        ----------
        message_id: int
            Message id

        file_name: Optional[str]
            File name

        caption: Optional[str]
            Message caption

        Returns
        -------
        str
            File name
        """

        res: str = ""
        for prefix in self.file_name_prefix:
            if prefix == "message_id":
                if res != "":
                    res += self.file_name_prefix_split
                res += f"{message_id}"
            elif prefix == "file_name" and file_name:
                if res != "":
                    res += self.file_name_prefix_split
                res += f"{file_name}"
            elif prefix == "caption" and caption:
                if res != "":
                    res += self.file_name_prefix_split
                res += f"{caption}"
        if res == "":
            res = f"{message_id}"

        return validate_title(res)

    def need_skip_message(
        self, download_config: ChatDownloadConfig, message_id: int
    ) -> bool:
        """if need skip download message.

        Parameters
        ----------
        chat_id: str
            Config.yaml defined

        message_id: int
            Readily to download message id
        Returns
        -------
        bool
        """
        if message_id in download_config.ids_to_retry_dict:
            return True

        return False

    def exec_filter(self, download_config: ChatDownloadConfig, meta_data: MetaData):
        """
        Executes the filter on the given download configuration.

        Args:
            download_config (ChatDownloadConfig): The download configuration object.
            meta_data (MetaData): The meta data object.

        Returns:
            bool: The result of executing the filter.
        """
        if download_config.download_filter:
            self.download_filter.set_meta_data(meta_data)
            return self.download_filter.exec(download_config.download_filter)

        return True

    # pylint: disable = R0912
    def update_config(self, immediate: bool = True):
        """update config

        Parameters
        ----------
        immediate: bool
            If update config immediate,default True
        """
        # TODO: fix this not exist chat
        if not self.app_data.get("chat") and self.config.get("chat"):
            self.app_data["chat"] = [
                {"chat_id": i} for i in range(0, len(self.config["chat"]))
            ]
        idx = 0
        # pylint: disable = R1733
        for key, value in self.chat_download_config.items():
            # pylint: disable = W0201
            unfinished_ids = set(value.ids_to_retry)

            for it in value.ids_to_retry:
                if value.node.download_status.get(
                    it, DownloadStatus.FailedDownload
                ) in [DownloadStatus.SuccessDownload, DownloadStatus.SkipDownload]:
                    unfinished_ids.remove(it)

            for _idx, _value in value.node.download_status.items():
                if _value not in (
                    DownloadStatus.SuccessDownload,
                    DownloadStatus.SkipDownload,
                ):
                    unfinished_ids.add(_idx)

            self.chat_download_config[key].ids_to_retry = list(unfinished_ids)

            if idx >= len(self.app_data["chat"]):
                self.app_data["chat"].append({})

            if value.finish_task:
                # The Web UI form can change the chat list without a restart;
                # the running task nodes still refer to the old order, so align
                # the config list defensively instead of indexing out of range.
                config_chats = self.config.setdefault("chat", [])
                while len(config_chats) <= idx:
                    config_chats.append({})
                config_chats[idx]["last_read_message_id"] = (
                    value.last_read_message_id + 1
                )

            self.app_data["chat"][idx]["chat_id"] = key
            self.app_data["chat"][idx]["ids_to_retry"] = value.ids_to_retry
            idx += 1

        self.config["save_path"] = self.save_path
        self.config["file_path_prefix"] = self.file_path_prefix

        if self.config.get("ids_to_retry"):
            self.config.pop("ids_to_retry")

        if self.config.get("chat_id"):
            self.config.pop("chat_id")

        if self.config.get("download_filter"):
            self.config.pop("download_filter")

        if self.config.get("last_read_message_id"):
            self.config.pop("last_read_message_id")

        self.config["language"] = self.language.name
        # for it in self.downloaded_ids:
        #    self.already_download_ids_set.add(it)

        # self.app_data["already_download_ids"] = list(self.already_download_ids_set)
        self.config["filter_advertisement_list"] = self.filter_advertisement_list
        self.config["replace_advertisement_list"] = self.replace_advertisement_list
        self.config["group_add_advertisement"] = self.group_add_advertisement

        if immediate:
            base = os.path.abspath(".")
            config_buffer = io.StringIO()
            _yaml.dump(self.config, config_buffer)
            write_text_atomic(
                os.path.join(base, self.config_file), config_buffer.getvalue()
            )

            data_buffer = io.StringIO()
            _yaml.dump(self.app_data, data_buffer)
            write_text_atomic(
                os.path.join(base, self.app_data_file), data_buffer.getvalue()
            )

    #: Every media type the downloader understands (mirrors the Web UI form).
    MEDIA_TYPES = tuple(name for name, _ in MEDIA_TYPE_OPTIONS)

    @classmethod
    def _normalize_media_types(cls, raw) -> List[str]:
        """Always keep ``media_types`` as a list of known media types.

        The raw YAML editor (and a hand edit) used to be accepted verbatim, so
        ``media_types: video`` turned into ``for media_type in "video"`` and
        every message was silently skipped.
        """
        if isinstance(raw, str):
            items = parse_format_list(raw)
        elif isinstance(raw, (list, tuple)):
            items = [str(item).strip() for item in raw if str(item).strip()]
        else:
            items = []

        known = [item for item in items if item in cls.MEDIA_TYPES]
        if known:
            unknown = [item for item in items if item not in cls.MEDIA_TYPES]
            if unknown:
                logger.warning(
                    f"media_types 中未识别的类型 {unknown} 已忽略；"
                    f"可选值：{list(cls.MEDIA_TYPES)}"
                )
            return known

        logger.warning(
            f"media_types 配置无效（{raw!r}），已按全部媒体类型处理。"
        )
        return list(cls.MEDIA_TYPES)

    @staticmethod
    def _normalize_file_formats(raw) -> dict:
        """Always keep ``file_formats`` values as lists.

        Older configs (and a hand edit, or the first version of the config form)
        stored a string such as ``"['all']"`` or ``"mp3,flac"``. ``_can_download``
        treats the value as a list, so a string silently filtered every file out
        — the bug behind "everything is suddenly skipped".
        """
        formats: dict = {"audio": ["all"], "document": ["all"], "video": ["all"]}
        if not isinstance(raw, dict):
            logger.warning(
                "config.yaml 缺少 file_formats，已按全部格式处理。"
            )
            return formats

        repaired = False
        for media_type, value in raw.items():
            if isinstance(value, (list, tuple)):
                items = [str(item) for item in value if str(item).strip()]
            elif isinstance(value, str):
                items = parse_format_list(value)
                repaired = True
            else:
                items = []
            formats[str(media_type)] = items or ["all"]

        if repaired:
            logger.warning(
                "config.yaml 的 file_formats 存在文本写法（如 \"['all']\"），"
                "已自动转换为列表；请到 Web UI 重新保存一次以修正文件。"
            )
        return formats

    def reload_hot_config(self, config: dict) -> Tuple[List[str], List[str]]:
        """Apply the settings that can change without restarting the process.

        The Web UI config editor calls this after saving ``config.yaml``.
        Anything that would rebuild the client, the task nodes or the HTTP
        listener is reported back as "restart required" instead of being
        silently ignored.

        Returns
        -------
        Tuple[List[str], List[str]]
            (applied keys, keys that need a restart)
        """
        applied: List[str] = []
        restart: List[str] = []

        # Keep the in-memory copy in sync so shutdown persistence writes the
        # values the operator just saved.
        self.config = config

        if "media_types" in config:
            normalized_types = self._normalize_media_types(config["media_types"])
            if normalized_types != self.media_types:
                self.media_types = normalized_types
                applied.append("media_types")

        if "file_formats" in config:
            normalized = self._normalize_file_formats(config["file_formats"])
            if normalized != self.file_formats:
                self.file_formats = normalized
                applied.append("file_formats")

        if "file_path_prefix" in config:
            self.file_path_prefix = list(config["file_path_prefix"])
            applied.append("file_path_prefix")

        if "file_name_prefix" in config:
            self.file_name_prefix = list(config["file_name_prefix"])
            applied.append("file_name_prefix")

        if "file_name_prefix_split" in config:
            self.file_name_prefix_split = str(config["file_name_prefix_split"])
            applied.append("file_name_prefix_split")

        if "hide_file_name" in config:
            self.hide_file_name = bool(config["hide_file_name"])
            applied.append("hide_file_name")

        if "enable_download_txt" in config:
            self.enable_download_txt = bool(config["enable_download_txt"])
            applied.append("enable_download_txt")

        if "after_upload_telegram_delete" in config:
            self.after_upload_telegram_delete = bool(
                config["after_upload_telegram_delete"]
            )
            applied.append("after_upload_telegram_delete")

        if "drop_no_audio_video" in config:
            self.drop_no_audio_video = bool(config["drop_no_audio_video"])
            applied.append("drop_no_audio_video")

        if "log_level" in config:
            self.log_level = str(config["log_level"])
            applied.append("log_level")

        if "max_concurrent_chunks" in config:
            # New downloads pick this up immediately; ranges already in flight
            # keep the split they started with.
            self.max_concurrent_chunks = set_chunk_concurrency(
                config["max_concurrent_chunks"]
            )
            applied.append("max_concurrent_chunks")

        if "date_format" in config:
            try:
                datetime(2023, 10, 31).strftime(str(config["date_format"]))
            except Exception as e:  # pylint: disable = W0703
                logger.warning(f"config date format error: {e}")
            else:
                self.date_format = str(config["date_format"])
                applied.append("date_format")

        if "language" in config:
            try:
                language = Language[str(config["language"]).upper()]
            except KeyError:
                logger.warning(f"unknown language: {config['language']}")
            else:
                self.set_language(language)
                applied.append("language")

        if "filter_advertisement_list" in config:
            self.filter_advertisement_list = (
                yaml.comments.CommentedSeq(config["filter_advertisement_list"])
                if config["filter_advertisement_list"]
                else yaml.comments.CommentedSeq([])
            )
            applied.append("filter_advertisement_list")

        if "replace_advertisement_list" in config:
            self.replace_advertisement_list = (
                yaml.comments.CommentedSeq(config["replace_advertisement_list"])
                if config["replace_advertisement_list"]
                else yaml.comments.CommentedSeq([])
            )
            applied.append("replace_advertisement_list")

        if "group_add_advertisement" in config:
            self.group_add_advertisement = dict(
                config["group_add_advertisement"] or {}
            )
            applied.append("group_add_advertisement")

        if "web_login_secret" in config:
            new_secret = str(config["web_login_secret"] or "")
            if not new_secret and self.web_login_secret:
                # The HTTP listener is already bound (possibly to 0.0.0.0) and
                # the "no password -> loopback only" guard runs at startup only.
                # Clearing the password at runtime would expose an
                # unauthenticated Web UI to the network until the next restart,
                # so keep the old password and ask for a restart instead.
                restart.append("web_login_secret")
            elif new_secret != self.web_login_secret:
                self.web_login_secret = new_secret
                applied.append("web_login_secret")

        upload_drive = config.get("upload_drive")
        if isinstance(upload_drive, dict):
            drive = self.cloud_drive_config
            if "enable_upload_file" in upload_drive:
                drive.enable_upload_file = bool(upload_drive["enable_upload_file"])
            if upload_drive.get("upload_adapter"):
                drive.upload_adapter = upload_drive["upload_adapter"]
            if upload_drive.get("remote_dir"):
                drive.remote_dir = upload_drive["remote_dir"]
            if upload_drive.get("rclone_path"):
                drive.rclone_path = upload_drive["rclone_path"]
            if "before_upload_file_zip" in upload_drive:
                drive.before_upload_file_zip = bool(
                    upload_drive["before_upload_file_zip"]
                )
            if "after_upload_file_delete" in upload_drive:
                drive.after_upload_file_delete = bool(
                    upload_drive["after_upload_file_delete"]
                )
            drive.dir_cache.clear()
            applied.append("upload_drive")

        # Everything below builds a client, a task node or the HTTP listener at
        # startup, so a running process can only pick it up after a restart.
        for key in (
            "api_id",
            "api_hash",
            "bot_token",
            "chat",
            "chat_id",
            "proxy",
            "save_path",
            "web_host",
            "web_port",
            "max_download_task",
            "max_concurrent_transmissions",
            "allowed_user_ids",
            "forward_limit",
            "start_timeout",
            "restart_program",
            "debug_web",
        ):
            if key in config:
                restart.append(key)

        return applied, restart

    def set_language(self, language: Language):
        """Set Language"""
        self.language = language
        set_language(language)

    def load_config(self):
        """Load user config"""
        config_path = os.path.join(os.path.abspath("."), self.config_file)
        self._ensure_config_file(config_path)
        with open(config_path, encoding="utf-8") as f:
            config = _yaml.load(f.read())
            if config:
                self.config = config
                self.assign_config(self.config)

        app_data_path = os.path.join(os.path.abspath("."), self.app_data_file)
        if os.path.isdir(app_data_path):
            # docker-compose bind-mounts this path; if the host file did not
            # exist, Docker created a *directory* here instead of a file.
            logger.warning(
                f"{self.app_data_file} 是一个目录而不是文件（很可能是 Docker 挂载时"
                f"宿主机上还没有这个文件），已忽略；请 touch {self.app_data_file}"
            )
        elif os.path.exists(app_data_path):
            with open(
                app_data_path,
                encoding="utf-8",
            ) as f:
                app_data = _yaml.load(f.read())
                if app_data:
                    self.app_data = app_data
                    self.assign_app_data(self.app_data)

    #: ``config.yaml`` holds api_hash / bot_token / proxy passwords and is
    #: git-ignored; the repository tracks this template instead.
    CONFIG_TEMPLATE_FILE = "config.yaml.example"

    @classmethod
    def _ensure_config_file(cls, config_path: str) -> None:
        """Create ``config.yaml`` from the tracked template on first run.

        A fresh checkout has no ``config.yaml`` (it is git-ignored), so without
        this the process died with a bare ``FileNotFoundError``.
        """
        if os.path.isdir(config_path):
            # Same Docker footgun as data.yaml: a missing bind-mount source
            # becomes a directory, and the next ``open()`` would only say
            # "IsADirectoryError" without explaining how to fix it.
            raise IsADirectoryError(
                f"{config_path} 是一个目录而不是文件；请删掉它并执行 "
                f"`cp {cls.CONFIG_TEMPLATE_FILE} {os.path.basename(config_path)}`"
            )
        if os.path.exists(config_path):
            return
        template_path = os.path.join(
            os.path.dirname(config_path) or ".", cls.CONFIG_TEMPLATE_FILE
        )
        if not os.path.exists(template_path):
            return
        shutil.copyfile(template_path, config_path)
        logger.warning(
            f"config.yaml 不存在，已从 {cls.CONFIG_TEMPLATE_FILE} 创建；"
            "请填写 api_id / api_hash / chat 后重新启动。"
        )

    def pre_run(self):
        """before run application do"""
        self.cloud_drive_config.pre_run()
        if not os.path.exists(self.session_file_path):
            os.makedirs(self.session_file_path)
        set_language(self.language)

    def is_match_advertisement(self, caption) -> bool:
        """is match advertisement

        Parameters
        ----------
        caption: str
        """
        for ad in self.filter_advertisement_list:
            if ad in caption:
                return True

        return False

    def set_caption_name(
        self, chat_id: Union[int, str], media_group_id: Optional[str], caption: str
    ):
        """set caption name map

        Parameters
        ----------
        chat_id: str
            Unique identifier for this chat.

        media_group_id: Optional[str]
            The unique identifier of a media message group this message belongs to.

        caption: str
            Caption for the audio, document, photo, video or voice, 0-1024 characters.
        """
        if not media_group_id:
            return

        if chat_id in self.caption_name_dict:
            self.caption_name_dict[chat_id][media_group_id] = caption
        else:
            self.caption_name_dict[chat_id] = {media_group_id: caption}

    def get_caption_name(
        self, chat_id: Union[int, str], media_group_id: Optional[str]
    ) -> Optional[str]:
        """set caption name map
                media_group_id: Optional[str]
            The unique identifier of a media message group this message belongs to.

        caption: str
            Caption for the audio, document, photo, video or voice, 0-1024 characters.
        """

        if (
            not media_group_id
            or chat_id not in self.caption_name_dict
            or media_group_id not in self.caption_name_dict[chat_id]
        ):
            return None

        return str(self.caption_name_dict[chat_id][media_group_id])

    def set_caption_entities(
        self, chat_id: Union[int, str], media_group_id: Optional[str], caption_entities
    ):
        """
        set caption entities map
        """
        if not media_group_id:
            return

        if chat_id in self.caption_entities_dict:
            self.caption_entities_dict[chat_id][media_group_id] = caption_entities
        else:
            self.caption_entities_dict[chat_id] = {media_group_id: caption_entities}

    def get_caption_entities(
        self, chat_id: Union[int, str], media_group_id: Optional[str]
    ):
        """
        get caption entities map
        """
        if (
            not media_group_id
            or chat_id not in self.caption_entities_dict
            or media_group_id not in self.caption_entities_dict[chat_id]
        ):
            return None

        return self.caption_entities_dict[chat_id][media_group_id]

    def set_download_id(
        self, node: TaskNode, message_id: int, download_status: DownloadStatus
    ):
        """Set Download status"""
        if download_status is DownloadStatus.SuccessDownload:
            self.total_download_task += 1

        if node.chat_id not in self.chat_download_config:
            return

        self.chat_download_config[node.chat_id].finish_task += 1

        self.chat_download_config[node.chat_id].last_read_message_id = max(
            self.chat_download_config[node.chat_id].last_read_message_id, message_id
        )
