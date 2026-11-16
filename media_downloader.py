"""Downloads media from telegram."""
import asyncio
import logging
import os
import shutil
import time
from typing import List, Optional, Tuple, Union

import pyrogram
from loguru import logger
from pyrogram.types import Audio, Document, Photo, Video, VideoNote, Voice
from rich.logging import RichHandler

from module.app import Application, ChatDownloadConfig, DownloadStatus, TaskNode
from module.bot import start_download_bot, stop_download_bot
from module.cloud_drive import UPLOAD_EXISTS
from module.config_form import parse_format_list
from module.download_stat import (
    TaskCancelled,
    clear_task_cancelled,
    get_failed_downloads,
    is_task_cancelled,
    mark_download_completed,
    mark_download_failed,
    mark_download_retried,
    normalize_chat_id,
    remove_download_result,
    update_download_status,
)
from module.fast_download import describe_chunk_settings, set_chunk_concurrency
from module.get_chat_history_v2 import get_chat_history_v2
from module.language import _t
from module.pyrogram_extension import (
    HookClient,
    fetch_message,
    get_extension,
    record_download_status,
    report_bot_download_status,
    set_max_concurrent_transmissions,
    set_meta_data,
    update_cloud_upload_stat,
    upload_telegram_chat,
)
from module.pyrogram_guard import DownloadIncomplete, install_get_file_guard
from module.web import init_web, set_retry_handler
from utils.format import truncate_filename, validate_title
from utils.log import LogFilter
from utils.meta import print_meta
from utils.meta_data import MetaData

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler()],
)

CONFIG_NAME = "config.yaml"
DATA_FILE_NAME = "data.yaml"
APPLICATION_NAME = "media_downloader"
app = Application(CONFIG_NAME, DATA_FILE_NAME, APPLICATION_NAME)

queue: asyncio.Queue = asyncio.Queue()
RETRY_TIME_OUT = 3

logging.getLogger("pyrogram.session.session").addFilter(LogFilter())
logging.getLogger("pyrogram.client").addFilter(LogFilter())

logging.getLogger("pyrogram").setLevel(logging.WARNING)


def _check_download_finish(media_size: int, download_path: str, ui_file_name: str):
    """Check download task if finish

    Parameters
    ----------
    media_size: int
        The size of the downloaded resource
    download_path: str
        Resource download hold path
    ui_file_name: str
        Really show file name

    """
    download_size = os.path.getsize(download_path)
    if media_size == download_size:
        logger.success(f"{_t('Successfully downloaded')} - {ui_file_name}")
    else:
        logger.warning(
            f"{_t('Media downloaded with wrong size')}: "
            f"{download_size}, {_t('actual')}: "
            f"{media_size}, {_t('file name')}: {ui_file_name}"
        )
        os.remove(download_path)
        # Report the real numbers instead of pretending the file reference
        # expired (the old code raised BadRequest here, which is why a
        # truncated transfer was logged as "file reference expired").
        raise DownloadIncomplete(download_size, media_size)


def _move_to_download_path(temp_download_path: str, download_path: str):
    """Move file to download path

    Parameters
    ----------
    temp_download_path: str
        Temporary download path

    download_path: str
        Download path

    """

    directory, _ = os.path.split(download_path)
    os.makedirs(directory, exist_ok=True)
    shutil.move(temp_download_path, download_path)


def _check_timeout(retry: int, _: int):
    """Check if message download timeout, then add message id into failed_ids

    Parameters
    ----------
    retry: int
        Retry download message times

    message_id: int
        Try to download message 's id

    """
    if retry == 2:
        return True
    return False


def _retry_delay(retry: int, network: bool = False) -> float:
    """Exponential backoff between download attempts.

    A failed ``upload.GetFile`` usually means the route to the Telegram storage
    node is unhealthy; retrying immediately only burns through the queue. Give
    DC/timeout failures a longer base so a temporarily overloaded storage node
    has time to recover before the task is marked as failed.
    """
    base = 10.0 if network else float(RETRY_TIME_OUT)
    return min(60.0, base * (2**retry))


def _is_network_cause(exc: Optional[BaseException]) -> bool:
    """Whether a download failure came from the Telegram/network layer."""
    if exc is None:
        return False
    if isinstance(exc, (OSError, ConnectionError, asyncio.TimeoutError)):
        return True
    try:
        import pyrogram  # pylint: disable = C0415

        if isinstance(exc, pyrogram.errors.RPCError):
            code = getattr(exc, "CODE", None)
            return isinstance(code, int) and code >= 500
    except Exception:  # pylint: disable = W0703
        pass
    return False


def _describe_download_error(
    incomplete: DownloadIncomplete, cause: Optional[BaseException]
) -> str:
    """Human readable reason stored in the failed-task list."""
    reason = str(incomplete)
    if _is_flood_cause(cause):
        hint = "Telegram 限流（免费账号的下载配额），可降低同时下载数、稍后重试，或改用 Premium 账号"
        return f"{reason}; {hint}"
    if _is_network_cause(cause):
        return f"{reason}; {_t('download failed due to network timeout')}"
    return reason


def _flood_wait_seconds(error: BaseException) -> float:
    """Seconds Telegram asks us to wait (``FloodWait`` / ``FloodPremiumWait``)."""
    try:
        return max(float(getattr(error, "value", 0) or 0), 0.0)
    except (TypeError, ValueError):
        return 0.0


def _is_flood_cause(exc: Optional[BaseException]) -> bool:
    """Whether ``exc`` is Telegram telling the account to slow down."""
    if exc is None:
        return False
    try:
        import pyrogram  # pylint: disable = C0415

        return isinstance(exc, pyrogram.errors.exceptions.flood_420.Flood)
    except Exception:  # pylint: disable = W0703
        return False


def _format_candidates(mime_format: Optional[str], file_name: str) -> List[str]:
    """Spellings of one media object that may appear in ``file_formats``.

    Operators configure real extensions (``mp3``, ``docx``, ``mov``) while the
    mime type only exposes the subtype (``audio/mpeg`` -> ``mpeg``,
    ``application/vnd.openxmlformats-officedocument...`` -> the long vendor
    string). Matching only the subtype silently skipped every file for such a
    config, so both the extension and the subtype are accepted.
    """
    candidates: List[str] = []
    suffix = os.path.splitext(file_name or "")[1].lstrip(".").lower()
    if suffix:
        candidates.append(suffix)
    if mime_format:
        candidates.append(str(mime_format).lower())
    return candidates


def _can_download(
    _type: str,
    file_formats: dict,
    file_format: Union[str, List[str], None],
) -> bool:
    """
    Check if the given file format can be downloaded.

    Parameters
    ----------
    _type: str
        Type of media object.
    file_formats: dict
        Dictionary containing the list of file_formats
        to be downloaded for `audio`, `document` & `video`
        media types
    file_format: str | list
        Format of the current file to be downloaded. A list holds every
        accepted spelling (extension and mime subtype).

    Returns
    -------
    bool
        True if the file format can be downloaded else False.
    """
    if _type in ["audio", "document", "video"]:
        allowed_formats = None
        if isinstance(file_formats, dict):
            allowed_formats = file_formats.get(_type)
        # Older/edited configs stored a string here; a missing or malformed
        # value must never silently drop every download.
        if isinstance(allowed_formats, str):
            allowed_formats = parse_format_list(allowed_formats)
        if not allowed_formats:
            return True
        allowed = {str(item).strip().lower() for item in allowed_formats}
        if "all" in allowed:
            return True
        if isinstance(file_format, str):
            candidates = {file_format.strip().lower()}
        else:
            candidates = {str(item).strip().lower() for item in (file_format or [])}
        return bool(candidates & allowed)
    return True


def _is_exist(file_path: str) -> bool:
    """
    Check if a file exists and it is not a directory.

    Parameters
    ----------
    file_path: str
        Absolute path of the file to be checked.

    Returns
    -------
    bool
        True if the file exists else False.
    """
    return not os.path.isdir(file_path) and os.path.exists(file_path)


# pylint: disable = R0912


async def _get_media_meta(
    chat_id: Union[int, str],
    message: pyrogram.types.Message,
    media_obj: Union[Audio, Document, Photo, Video, VideoNote, Voice],
    _type: str,
) -> Tuple[str, str, Optional[str]]:
    """Extract file name and file id from media object.

    Parameters
    ----------
    media_obj: Union[Audio, Document, Photo, Video, VideoNote, Voice]
        Media object to be extracted.
    _type: str
        Type of media object.

    Returns
    -------
    Tuple[str, str, Optional[str]]
        file_name, file_format
    """
    if _type in ["audio", "document", "video"]:
        # pylint: disable = C0301
        file_format: Optional[str] = media_obj.mime_type.split("/")[-1]  # type: ignore
    else:
        file_format = None

    file_name = None
    temp_file_name = None
    dirname = validate_title(f"{chat_id}")
    if message.chat and message.chat.title:
        dirname = validate_title(f"{message.chat.title}")

    if message.date:
        datetime_dir_name = message.date.strftime(app.date_format)
    else:
        datetime_dir_name = "0"

    if _type in ["voice", "video_note"]:
        # pylint: disable = C0209
        file_format = media_obj.mime_type.split("/")[-1]  # type: ignore
        file_save_path = app.get_file_save_path(_type, dirname, datetime_dir_name)
        file_name = "{} - {}_{}.{}".format(
            message.id,
            _type,
            media_obj.date.isoformat(),  # type: ignore
            file_format,
        )
        file_name = validate_title(file_name)
        temp_file_name = os.path.join(app.temp_save_path, dirname, file_name)

        file_name = os.path.join(file_save_path, file_name)
    else:
        file_name = getattr(media_obj, "file_name", None)
        caption = getattr(message, "caption", None)

        file_name_suffix = ".unknown"
        if not file_name:
            file_name_suffix = get_extension(
                media_obj.file_id, getattr(media_obj, "mime_type", "")
            )
        else:
            # file_name = file_name.split(".")[0]
            _, file_name_without_suffix = os.path.split(os.path.normpath(file_name))
            file_name, file_name_suffix = os.path.splitext(file_name_without_suffix)
            if not file_name_suffix:
                file_name_suffix = get_extension(
                    media_obj.file_id, getattr(media_obj, "mime_type", "")
                )

        if caption:
            caption = validate_title(caption)
            app.set_caption_name(chat_id, message.media_group_id, caption)
            app.set_caption_entities(
                chat_id, message.media_group_id, message.caption_entities
            )
        else:
            caption = app.get_caption_name(chat_id, message.media_group_id)

        if not file_name and message.photo:
            file_name = f"{message.photo.file_unique_id}"

        gen_file_name = (
            app.get_file_name(message.id, file_name, caption) + file_name_suffix
        )

        file_save_path = app.get_file_save_path(_type, dirname, datetime_dir_name)

        temp_file_name = os.path.join(app.temp_save_path, dirname, gen_file_name)

        file_name = os.path.join(file_save_path, gen_file_name)
    return truncate_filename(file_name), truncate_filename(temp_file_name), file_format


async def add_download_task(
    message: pyrogram.types.Message,
    node: TaskNode,
):
    """Add Download task"""
    if message.empty:
        return False
    node.download_status[message.id] = DownloadStatus.Downloading
    await queue.put((message, node))
    node.total_task += 1
    return True


async def save_msg_to_file(
    app, chat_id: Union[int, str], message: pyrogram.types.Message
):
    """Write message text into file"""
    dirname = validate_title(
        message.chat.title if message.chat and message.chat.title else str(chat_id)
    )
    datetime_dir_name = message.date.strftime(app.date_format) if message.date else "0"

    file_save_path = app.get_file_save_path("msg", dirname, datetime_dir_name)
    file_name = os.path.join(
        app.temp_save_path,
        file_save_path,
        f"{app.get_file_name(message.id, None, None)}.txt",
    )

    os.makedirs(os.path.dirname(file_name), exist_ok=True)

    if _is_exist(file_name):
        return DownloadStatus.SkipDownload, None

    with open(file_name, "w", encoding="utf-8") as f:
        f.write(message.text or "")

    return DownloadStatus.SuccessDownload, file_name


async def download_task(
    client: pyrogram.Client, message: pyrogram.types.Message, node: TaskNode
):
    """Download and Forward media.

    The bookkeeping used to live entirely after the download/upload calls. Any
    exception in between (disk full, upload error, an unexpected Pyrogram RPC
    error) escaped to ``worker``, which only logs: the task never reached a
    terminal state, ``total_task`` never matched ``finish_task`` and a one-shot
    run could never exit. Finalize the task in ``finally`` instead.
    """

    download_status = DownloadStatus.FailedDownload
    file_name = None
    file_size = 0
    upload_ok = False
    upload_state = None

    try:
        download_status, file_name = await download_media(
            client, message, app.media_types, app.file_formats, node
        )

        # ``record_download_status`` used to be able to return Downloading for
        # a wedged cache entry; never let that become a non-terminal task.
        if download_status is DownloadStatus.Downloading:
            logger.warning(
                f"Message[{message.id}]: download returned Downloading; "
                f"recording as failed"
            )
            download_status = DownloadStatus.FailedDownload

        if app.enable_download_txt and message.text and not message.media:
            download_status, file_name = await save_msg_to_file(
                app, node.chat_id, message
            )

        file_size = os.path.getsize(file_name) if file_name else 0

        await upload_telegram_chat(
            client,
            node.upload_user if node.upload_user else client,
            app,
            node,
            message,
            download_status,
            file_name,
        )

        # rclone upload
        if (
            not node.upload_telegram_chat_id
            and file_name
            and download_status is DownloadStatus.SuccessDownload
        ):
            ui_file_name = file_name
            if app.hide_file_name:
                ui_file_name = f"****{os.path.splitext(file_name)[-1]}"
            upload_state = await app.upload_file(
                file_name, update_cloud_upload_stat, (node, message.id, ui_file_name)
            )
            if upload_state:
                node.upload_success_count += 1
                upload_ok = True
                if upload_state == UPLOAD_EXISTS:
                    node.upload_skipped_count += 1
    except Exception as e:  # pylint: disable = W0703
        logger.exception(f"Message[{message.id}] task failed: {e}")
        # If the download itself completed, keep Success: the file exists and
        # the cloud-upload retry path (P1) can pick it up later. Any other
        # stage means the message did not reach a terminal state.
        if download_status is not DownloadStatus.SuccessDownload:
            download_status = DownloadStatus.FailedDownload
    finally:
        # A message deleted from the Web UI raises TaskCancelled inside the
        # progress callback. Treat it as skipped: it must not show up as a
        # failure, and the queue must keep finishing.
        if is_task_cancelled(node.chat_id, message.id):
            clear_task_cancelled(node.chat_id, message.id)
            download_status = DownloadStatus.SkipDownload
            file_name = None

        if download_status is DownloadStatus.Downloading:
            download_status = DownloadStatus.FailedDownload

        node.download_status[message.id] = download_status

        # Consume the reason in both branches so a later success cannot leave a
        # stale entry behind.
        reason = ""
        try:
            reason = node.last_error.pop(message.id, "")
        except Exception:  # pylint: disable = W0703
            reason = ""

        if download_status is DownloadStatus.FailedDownload:
            # Upstream left the row in the Web UI at whatever percentage it
            # reached and never offered a way to retry or drop it. Remove the
            # stale row and record the failure for the "Failed" tab instead.
            remove_download_result(node.chat_id, message.id)
            mark_download_failed(node.chat_id, message.id, file_name or "", reason)
        elif download_status is DownloadStatus.SuccessDownload:
            # Move the row into the completed list. Leaving it in the
            # in-progress dict was why the Web UI kept showing a finished
            # message at 100% long after the bot card reported success.
            mark_download_completed(
                node.chat_id,
                message.id,
                file_name or "",
                file_size,
                upload_state if upload_ok else None,
            )
        else:
            remove_download_result(node.chat_id, message.id)

        if not node.bot:
            app.set_download_id(node, message.id, download_status)

        try:
            await report_bot_download_status(
                node.bot,
                node,
                download_status,
                file_size,
            )
        except Exception as report_err:  # pylint: disable = W0703
            logger.exception(f"Message[{message.id}] report status failed: {report_err}")


# pylint: disable = R0915,R0914


@record_download_status
async def download_media(
    client: pyrogram.client.Client,
    message: pyrogram.types.Message,
    media_types: List[str],
    file_formats: dict,
    node: TaskNode,
):
    """
    Download media from Telegram.

    Each of the files to download are retried 3 times with a
    delay of 5 seconds each.

    Parameters
    ----------
    client: pyrogram.client.Client
        Client to interact with Telegram APIs.
    message: pyrogram.types.Message
        Message object retrieved from telegram.
    media_types: list
        List of strings of media types to be downloaded.
        Ex : `["audio", "photo"]`
        Supported formats:
            * audio
            * document
            * photo
            * video
            * voice
    file_formats: dict
        Dictionary containing the list of file_formats
        to be downloaded for `audio`, `document` & `video`
        media types.

    Returns
    -------
    int
        Current message id.
    """

    # pylint: disable = R0912

    file_name: str = ""
    ui_file_name: str = ""
    task_start_time: float = time.time()
    media_size = 0
    _media = None
    try:
        # Refetching is required to refresh expired file references, but it can
        # also raise (FloodWait, deleted message, network error). Keep it inside
        # the try so a failure is recorded instead of escaping with the download
        # cache still marked as in progress.
        message = await fetch_message(client, message)
        for _type in media_types:
            _media = getattr(message, _type, None)
            if _media is None:
                continue
            file_name, temp_file_name, file_format = await _get_media_meta(
                node.chat_id, message, _media, _type
            )
            media_size = getattr(_media, "file_size", 0)

            ui_file_name = file_name
            if app.hide_file_name:
                ui_file_name = f"****{os.path.splitext(file_name)[-1]}"

            if _can_download(
                _type, file_formats, _format_candidates(file_format, file_name)
            ):
                if _is_exist(file_name):
                    file_size = os.path.getsize(file_name)
                    # Only a byte-exact match proves the file is complete. The
                    # old `file_size or ...` shortcut treated every non-empty
                    # leftover as finished, so a truncated file (older releases
                    # accepted partial streams) was skipped forever.
                    if file_size == media_size:
                        logger.info(
                            f"id={message.id} {ui_file_name} "
                            f"{_t('already download,download skipped')}.\n"
                        )

                        return DownloadStatus.SkipDownload, None
            else:
                return DownloadStatus.SkipDownload, None

            break
    except Exception as e:
        logger.error(
            f"Message[{message.id}]: "
            f"{_t('could not be downloaded due to following exception')}:\n[{e}].",
            exc_info=True,
        )
        return DownloadStatus.FailedDownload, None
    if _media is None:
        return DownloadStatus.SkipDownload, None

    message_id = message.id
    last_error: str = ""

    for retry in range(3):
        try:
            temp_download_path = await client.download_media(
                message,
                file_name=temp_file_name,
                progress=update_download_status,
                progress_args=(
                    message_id,
                    ui_file_name,
                    task_start_time,
                    node,
                    client,
                ),
            )

            if temp_download_path and isinstance(temp_download_path, str):
                _check_download_finish(media_size, temp_download_path, ui_file_name)
                await asyncio.sleep(0.5)
                _move_to_download_path(temp_download_path, file_name)
                # TODO: if not exist file size or media
                return DownloadStatus.SuccessDownload, file_name

            # No path came back: the client aborted and removed its temp file.
            # Pause before the next attempt instead of hammering the queue.
            last_error = _t("download aborted before the file was written")
            delay = _retry_delay(retry)
            logger.warning(
                f"Message[{message.id}]: {last_error}, "
                f"{_t('retrying after')} {delay:g} {_t('seconds')}"
            )
            await asyncio.sleep(delay)
        except pyrogram.errors.exceptions.bad_request_400.BadRequest:
            logger.warning(
                f"Message[{message.id}]: {_t('file reference expired, refetching')}..."
            )
            await asyncio.sleep(_retry_delay(retry))
            message = await fetch_message(client, message)
            last_error = _t("file reference expired")
            if _check_timeout(retry, message.id):
                # pylint: disable = C0301
                logger.error(
                    f"Message[{message.id}]: "
                    f"{_t('file reference expired for 3 retries, download skipped.')}"
                )
        except DownloadIncomplete as incomplete:
            # The fork swallowed the protocol error; keep its cause so a
            # [-500 TIMEOUT] on upload.GetFile is not reported as a generic
            # interruption. Network causes also get an actionable hint.
            last_error = _describe_download_error(incomplete, incomplete.cause)
            delay = _retry_delay(retry, network=_is_network_cause(incomplete.cause))
            logger.warning(
                f"Message[{message.id}]: {last_error}; "
                f"{_t('retrying after')} {delay:g} {_t('seconds')}"
            )
            await asyncio.sleep(delay)
            # A truncated stream can also be a stale file reference that the
            # fork swallowed; refetch so the next attempt gets a fresh one.
            try:
                message = await fetch_message(client, message)
            except Exception as fetch_err:  # pylint: disable = W0703
                logger.warning(f"Message[{message.id}]: refetch failed: {fetch_err}")
            if _check_timeout(retry, message.id):
                logger.error(
                    f"Message[{message.id}]: {last_error}; "
                    f"{_t('download incomplete after 3 retries, download skipped.')}"
                )
        except pyrogram.errors.exceptions.flood_420.Flood as flood_err:
            # Covers FloodWait and FloodPremiumWait ("wait N seconds, or buy
            # Premium"). Sleeping keeps the retry budget instead of failing the
            # file outright; the bytes already downloaded are resumed by the
            # get_file guard.
            wait = _flood_wait_seconds(flood_err)
            logger.warning(
                f"Message[{message.id}]: Telegram 限速 "
                f"({type(flood_err).__name__})，等待 {wait:g} 秒后继续"
            )
            await asyncio.sleep(wait + 1)
            _check_timeout(retry, message.id)
        except TypeError:
            # pylint: disable = C0301
            logger.warning(
                f"{_t('Timeout Error occurred when downloading Message')}[{message.id}], "
                f"{_t('retrying after')} {RETRY_TIME_OUT} {_t('seconds')}"
            )
            await asyncio.sleep(RETRY_TIME_OUT)
            if _check_timeout(retry, message.id):
                logger.error(
                    f"Message[{message.id}]: {_t('Timing out after 3 reties, download skipped.')}"
                )
        except TaskCancelled:
            # The Web UI deleted this task while it was transferring. Stop
            # quietly; download_task turns it into a skip.
            logger.info(f"Message[{message.id}]: download cancelled from the Web UI")
            return DownloadStatus.FailedDownload, None
        except Exception as e:
            # pylint: disable = C0301
            logger.error(
                f"Message[{message.id}]: "
                f"{_t('could not be downloaded due to following exception')}:\n[{e}].",
                exc_info=True,
            )
            last_error = f"{e.__class__.__name__}: {e}"
            break

    if last_error:
        try:
            node.last_error[message.id] = last_error
        except Exception:  # pylint: disable = W0703
            pass
    return DownloadStatus.FailedDownload, None


def _load_config():
    """Load config"""
    app.load_config()


def _check_config() -> bool:
    """Check config"""
    print_meta(logger)
    try:
        _load_config()
        logger.add(
            os.path.join(app.log_file_path, "tdl.log"),
            rotation="10 MB",
            retention="10 days",
            level=app.log_level,
        )
    except Exception as e:
        logger.exception(f"load config error: {e}")
        return False

    return True


async def worker(client: pyrogram.client.Client):
    """Work for download task"""
    while app.is_running:
        try:
            item = await queue.get()
            message = item[0]
            node: TaskNode = item[1]

            if node.is_stop_transmission:
                continue

            if node.client:
                await download_task(node.client, message, node)
            else:
                await download_task(client, message, node)
        except Exception as e:
            logger.exception(f"{e}")


async def download_chat_task(
    client: pyrogram.Client,
    chat_download_config: ChatDownloadConfig,
    node: TaskNode,
):
    """Download all task"""
    if not node.chat_id:
        logger.warning(
            "chat_id 为空，跳过该频道的自动下载；请在 config.yaml 或 Web UI 中补全。"
        )
        return

    messages_iter = get_chat_history_v2(
        client,
        node.chat_id,
        limit=node.limit,
        max_id=node.end_offset_id,
        offset_id=chat_download_config.last_read_message_id,
        reverse=True,
    )

    chat_download_config.node = node
    # Scanning a full history can take minutes. Mark the task active for that
    # phase too, otherwise the bot card only appears once the scan is over and a
    # long "download from message N to the end" looks like it never started.
    node.is_scanning = True

    try:
        if chat_download_config.ids_to_retry:
            logger.info(f"{_t('Downloading files failed during last run')}...")
            skipped_messages: list = await client.get_messages(  # type: ignore
                chat_id=node.chat_id, message_ids=chat_download_config.ids_to_retry
            )

            for message in skipped_messages:
                await add_download_task(message, node)

        async for message in messages_iter:  # type: ignore
            meta_data = MetaData()

            caption = message.caption
            if caption:
                caption = validate_title(caption)
                app.set_caption_name(node.chat_id, message.media_group_id, caption)
                app.set_caption_entities(
                    node.chat_id, message.media_group_id, message.caption_entities
                )
            else:
                caption = app.get_caption_name(node.chat_id, message.media_group_id)
            set_meta_data(meta_data, message, caption)

            if app.need_skip_message(chat_download_config, message.id):
                continue

            if app.exec_filter(chat_download_config, meta_data):
                await add_download_task(message, node)
            else:
                node.download_status[message.id] = DownloadStatus.SkipDownload
                if message.media_group_id:
                    await upload_telegram_chat(
                        client,
                        node.upload_user,
                        app,
                        node,
                        message,
                        DownloadStatus.SkipDownload,
                    )
    finally:
        chat_download_config.need_check = True
        chat_download_config.total_task = node.total_task
        node.is_scanning = False
        node.is_running = True


async def download_all_chat(client: pyrogram.Client):
    """Download All chat"""
    for key, value in app.chat_download_config.items():
        value.node = TaskNode(chat_id=key)
        try:
            await download_chat_task(client, value, value.node)
        except Exception as e:
            logger.warning(f"Download {key} error: {e}")
        finally:
            value.need_check = True


_retry_client: Optional["HookClient"] = None


async def retry_failed_messages(
    chat_id: Union[int, str, None] = None, message_id: Optional[int] = None
) -> Tuple[int, List[str]]:
    """Re-queue failed messages without restarting the process.

    Returns ``(queued_count, reasons)``; the Web UI shows the first reason when
    nothing could be queued (message deleted, no permission, ...). Failures are
    also kept in ``data.yaml`` (``ids_to_retry``) and re-queued on the next
    start, so this is the fast path, not the only path.
    """
    if _retry_client is None:
        logger.warning("retry requested before the client is ready")
        return 0, ["下载客户端尚未就绪，请稍后再试"]

    chat_id = normalize_chat_id(chat_id)
    failed = get_failed_downloads()
    targets = []
    for cid, messages in failed.items():
        if chat_id is not None and str(cid) != str(chat_id):
            continue
        ids = [
            mid for mid in messages if message_id is None or mid == message_id
        ]
        if ids:
            targets.append((normalize_chat_id(cid), ids))

    if not targets:
        logger.info("no failed message matched the retry request")
        return 0, ["失败记录里没有这条任务（可能已重试成功或被清理）"]

    retried = 0
    reasons: List[str] = []
    for cid, ids in targets:
        chat_config = app.chat_download_config.get(cid)
        if chat_config is None:
            chat_config = app.chat_download_config.get(str(cid))
        if chat_config is not None:
            node = chat_config.node
        else:
            # Direct-download failures (bot /download of an arbitrary link) have
            # no chat config entry; a throwaway node still lets the queue work.
            logger.info(
                f"retry: chat {cid} is not configured, using a temporary task node"
            )
            node = TaskNode(chat_id=cid)

        try:
            messages = await _retry_client.get_messages(  # type: ignore
                chat_id=cid, message_ids=ids
            )
        except Exception as e:  # pylint: disable = W0703
            logger.warning(f"retry fetch {cid} failed: {e}")
            reasons.append(f"拉取消息失败：{e}")
            continue

        for message in messages:
            if not message or getattr(message, "empty", False):
                logger.warning(f"retry: message {ids} in {cid} is not accessible")
                reasons.append(
                    f"消息 {', '.join(str(one) for one in ids)} 已不可访问"
                    "（可能被删除或当前账号没有权限）"
                )
                continue
            if await add_download_task(message, node):
                retried += 1
                mark_download_retried(cid, message.id)
            else:
                reasons.append(f"消息 {message.id} 无法重新加入队列")

    logger.info(f"requeued {retried} failed message(s)")
    return retried, reasons


async def run_until_all_task_finish():
    """Normal download"""
    while True:
        finish: bool = True
        for _, value in app.chat_download_config.items():
            if not value.need_check or value.total_task != value.finish_task:
                finish = False

        if (not app.bot_token and finish) or app.restart_program:
            break

        await asyncio.sleep(1)


def _exec_loop():
    """Exec loop"""

    app.loop.run_until_complete(run_until_all_task_finish())


async def start_server(client: pyrogram.Client):
    """
    Start the server using the provided client.
    """
    await client.start()


async def stop_server(client: pyrogram.Client):
    """
    Stop the server using the provided client.
    """
    await client.stop()


def main():
    """Main function of the downloader."""
    # pylint: disable = W0603
    global _retry_client
    # The bundled Pyrogram fork silently ends the chunk stream when a
    # GetFile request fails; resume instead of accepting a partial file.
    install_get_file_guard()
    # One file is a single sequential chunk stream by default; the Web UI can
    # split it into concurrent ranges (module.fast_download).
    set_chunk_concurrency(app.max_concurrent_chunks)
    chunk_summary = describe_chunk_settings()
    if chunk_summary:
        logger.info(chunk_summary)
    tasks = []
    client = HookClient(
        "media_downloader",
        api_id=app.api_id,
        api_hash=app.api_hash,
        proxy=app.proxy,
        workdir=app.session_file_path,
        start_timeout=app.start_timeout,
        no_updates=True,
    )
    try:
        app.pre_run()
        init_web(app)

        # Every parallel range holds one transmission slot for its whole stream,
        # so size the pool for the worst case instead of serialising ranges.
        transmissions = max(
            app.max_concurrent_transmissions,
            app.max_download_task * app.max_concurrent_chunks,
        )
        if transmissions != app.max_concurrent_transmissions:
            logger.info(
                f"max_concurrent_transmissions {app.max_concurrent_transmissions} "
                f"raised to {transmissions} for {app.max_download_task} files × "
                f"{app.max_concurrent_chunks} ranges"
            )
        set_max_concurrent_transmissions(client, transmissions)

        app.loop.run_until_complete(start_server(client))
        logger.success(_t("Successfully started (Press Ctrl+C to stop)"))

        # Let the Web UI "Failed" tab put messages back into the queue.
        _retry_client = client
        set_retry_handler(
            lambda chat_id, message_id: asyncio.run_coroutine_threadsafe(
                retry_failed_messages(chat_id, message_id), app.loop
            )
        )

        # download_all_chat and the workers are cancelled together below; the
        # former used to be created without keeping a reference, so shutdown
        # left a pending task behind.
        tasks.append(app.loop.create_task(download_all_chat(client)))
        for _ in range(app.max_download_task):
            task = app.loop.create_task(worker(client))
            tasks.append(task)

        if app.bot_token:
            app.loop.run_until_complete(
                start_download_bot(app, client, add_download_task, download_chat_task)
            )
        _exec_loop()
    except KeyboardInterrupt:
        logger.info(_t("KeyboardInterrupt"))
    except Exception as e:
        logger.exception("{}", e)
    finally:
        app.is_running = False
        # Every shutdown step must run even if an earlier one fails, otherwise a
        # failed client.stop() would skip persisting config.yaml / data.yaml.
        if app.bot_token:
            try:
                app.loop.run_until_complete(stop_download_bot())
            except Exception as stop_err:  # pylint: disable = W0703
                logger.warning(f"stop bot error: {stop_err}")
        try:
            app.loop.run_until_complete(stop_server(client))
        except Exception as stop_err:  # pylint: disable = W0703
            logger.warning(f"stop client error: {stop_err}")
        for task in tasks:
            task.cancel()
        if tasks:
            # Await the cancellations, otherwise asyncio warns about pending
            # tasks that were destroyed at interpreter shutdown.
            app.loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        logger.info(_t("Stopped!"))
        # check_for_updates(app.proxy)
        logger.info(f"{_t('update config')}......")
        try:
            app.update_config()
        except OSError as save_err:
            # e.g. data.yaml exists as a directory because docker created the
            # bind-mount source; report it instead of crashing on the way out.
            logger.error(f"save config failed: {save_err}")
        logger.success(
            f"{_t('Updated last read message_id to config file')},"
            f"{_t('total download')} {app.total_download_task}, "
            f"{_t('total upload file')} "
            f"{app.cloud_drive_config.total_upload_success_file_count}"
        )


if __name__ == "__main__":
    if _check_config():
        main()
