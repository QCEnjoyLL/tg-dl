"""Download Stat"""
import asyncio
import os
import time
from enum import Enum

from pyrogram import Client

from module.app import TaskNode


class DownloadState(Enum):
    """Download state"""

    Downloading = 1
    StopDownload = 2


_download_result: dict = {}
_failed_downloads: dict = {}
_total_download_speed: int = 0
_total_download_size: int = 0
_last_download_time: float = time.time()
_download_state: DownloadState = DownloadState.Downloading


def get_download_result() -> dict:
    """get global download result"""
    return _download_result


def get_failed_downloads() -> dict:
    """Return a snapshot of failed downloads, grouped by chat id."""
    return {chat_id: dict(messages) for chat_id, messages in _failed_downloads.items()}


def mark_download_failed(
    chat_id, message_id: int, file_name: str = "", reason: str = ""
) -> None:
    """Remember a failed download so the Web UI can list and retry it."""
    _failed_downloads.setdefault(chat_id, {})[message_id] = {
        "file_name": os.path.basename(file_name) if file_name else "",
        "time": int(time.time()),
        "reason": reason,
    }


def mark_download_retried(chat_id, message_id: int) -> None:
    """Forget one failure after it was queued again."""
    messages = _failed_downloads.get(chat_id)
    if not messages:
        return
    messages.pop(message_id, None)
    if not messages:
        _failed_downloads.pop(chat_id, None)


def clear_failed_downloads(chat_id=None) -> int:
    """Forget every recorded failure, optionally for a single chat."""
    if chat_id is None:
        count = sum(len(messages) for messages in _failed_downloads.values())
        _failed_downloads.clear()
        return count

    return len(_failed_downloads.pop(chat_id, {}))


def remove_download_result(chat_id, message_id: int) -> None:
    """Drop a stale in-progress row from the Web UI list.

    A failed download used to stay in ``_download_result`` forever at the
    percentage it reached, usually 0%. The page then showed rows that could
    never move and could not be removed without restarting the process.

    The dictionaries are rebuilt and swapped in instead of mutated in place,
    because the Flask thread iterates them while this runs.
    """
    messages = _download_result.get(chat_id)
    if not messages or message_id not in messages:
        return

    remaining = dict(messages)
    remaining.pop(message_id, None)

    if remaining:
        _download_result[chat_id] = remaining
        return

    outer = dict(_download_result)
    outer.pop(chat_id, None)
    _download_result.clear()
    _download_result.update(outer)


def get_total_download_speed() -> int:
    """get total download speed"""
    return _total_download_speed


def get_download_state() -> DownloadState:
    """get download state"""
    return _download_state


# pylint: disable = W0603
def set_download_state(state: DownloadState):
    """set download state"""
    global _download_state
    _download_state = state


async def update_download_status(
    down_byte: int,
    total_size: int,
    message_id: int,
    file_name: str,
    start_time: float,
    node: TaskNode,
    client: Client,
):
    """update_download_status"""
    cur_time = time.time()
    # pylint: disable = W0603
    global _total_download_speed
    global _total_download_size
    global _last_download_time

    if node.is_stop_transmission:
        client.stop_transmission()

    chat_id = node.chat_id

    while get_download_state() == DownloadState.StopDownload:
        if node.is_stop_transmission:
            client.stop_transmission()
        await asyncio.sleep(1)

    if not _download_result.get(chat_id):
        _download_result[chat_id] = {}

    if _download_result[chat_id].get(message_id):
        last_download_byte = _download_result[chat_id][message_id]["down_byte"]
        last_time = _download_result[chat_id][message_id]["end_time"]
        download_speed = _download_result[chat_id][message_id]["download_speed"]
        each_second_total_download = _download_result[chat_id][message_id][
            "each_second_total_download"
        ]
        end_time = _download_result[chat_id][message_id]["end_time"]

        _total_download_size += down_byte - last_download_byte
        each_second_total_download += down_byte - last_download_byte

        if cur_time - last_time >= 1.0:
            download_speed = int(each_second_total_download / (cur_time - last_time))
            end_time = cur_time
            each_second_total_download = 0

        download_speed = max(download_speed, 0)

        _download_result[chat_id][message_id]["down_byte"] = down_byte
        _download_result[chat_id][message_id]["end_time"] = end_time
        _download_result[chat_id][message_id]["download_speed"] = download_speed
        _download_result[chat_id][message_id][
            "each_second_total_download"
        ] = each_second_total_download
    else:
        each_second_total_download = down_byte
        _download_result[chat_id][message_id] = {
            "down_byte": down_byte,
            "total_size": total_size,
            "file_name": file_name,
            "start_time": start_time,
            "end_time": cur_time,
            "download_speed": down_byte / (cur_time - start_time),
            "each_second_total_download": each_second_total_download,
            "task_id": node.task_id,
        }
        _total_download_size += down_byte

    if cur_time - _last_download_time >= 1.0:
        # update speed
        _total_download_speed = int(
            _total_download_size / (cur_time - _last_download_time)
        )
        _total_download_speed = max(_total_download_speed, 0)
        _total_download_size = 0
        _last_download_time = cur_time
