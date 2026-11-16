"""Download stat.

Progress state is written from the asyncio loop (Pyrogram progress callbacks)
and read from the Flask thread (the Web UI). Every mutation and snapshot goes
through ``_PROGRESS_LOCK``: iterating a dict while the other thread inserts keys
raised ``RuntimeError: dictionary changed size during iteration`` and returned
500s during normal downloads.

The module also owns the per-message controls the Web UI exposes:
``pause``/``resume`` (the progress callback waits) and ``cancel`` (the progress
callback raises, which aborts that single file instead of the whole client).
"""
import asyncio
import os
import threading
import time
from collections import OrderedDict
from enum import Enum
from typing import Optional

from pyrogram import Client

from module.app import TaskNode


class DownloadState(Enum):
    """Download state"""

    Downloading = 1
    StopDownload = 2


class TaskCancelled(Exception):
    """Raised from a progress callback to abort one queued message.

    Pyrogram awaits progress callbacks inside the transfer loop, so raising here
    unwinds ``client.download_media`` for that file only. The generic handler in
    ``download_media`` records the failure and the task finalizer in
    ``download_task`` turns a cancelled task into "skip" instead of "failed".
    """


#: How many finished downloads the Web UI keeps in memory. The old dict grew for
#: the whole process lifetime and was re-serialised on every poll.
MAX_COMPLETED_DOWNLOADS = 500

_PROGRESS_LOCK = threading.RLock()
_download_result: dict = {}
_completed_downloads: "OrderedDict" = OrderedDict()
_failed_downloads: dict = {}
_paused_tasks: set = set()
_cancelled_tasks: set = set()
_download_state: DownloadState = DownloadState.Downloading


def normalize_chat_id(chat_id):
    """Canonical chat id: int for numeric ids, the original string otherwise.

    The Web UI round-trips everything as text (``-100123``), while tasks and the
    YAML config use ints. Comparing the two without normalizing made every
    delete/retry a silent no-op, because the string key did not match the int
    key in the per-chat dicts.
    """
    if isinstance(chat_id, bool):
        return chat_id
    if isinstance(chat_id, int):
        return chat_id
    if isinstance(chat_id, str):
        text = chat_id.strip()
        if text.lstrip("-").isdigit():
            return int(text)
        return chat_id
    return chat_id


def _task_key(chat_id, message_id: int):
    """Normalize a task key.

    YAML chat ids are ints while ``@username`` chats are strings, and the Web UI
    round-trips everything as text. Keep a single canonical form per message.
    """
    return (str(normalize_chat_id(chat_id)), int(message_id))


def get_download_result() -> dict:
    """Return a snapshot of in-progress downloads, grouped by chat id.

    Both levels are copied: the Flask thread must never mutate (or observe a
    partially written) row that the asyncio loop is updating.
    """
    with _PROGRESS_LOCK:
        return {
            chat_id: {message_id: dict(row) for message_id, row in messages.items()}
            for chat_id, messages in _download_result.items()
        }


def get_completed_downloads() -> list:
    """Return finished downloads, newest first."""
    with _PROGRESS_LOCK:
        return [dict(row) for row in reversed(_completed_downloads.values())]


def get_failed_downloads() -> dict:
    """Return a snapshot of failed downloads, grouped by chat id."""
    with _PROGRESS_LOCK:
        return {chat_id: dict(messages) for chat_id, messages in _failed_downloads.items()}


def mark_download_failed(
    chat_id, message_id: int, file_name: str = "", reason: str = ""
) -> None:
    """Remember a failed download so the Web UI can list and retry it."""
    chat_id = normalize_chat_id(chat_id)
    with _PROGRESS_LOCK:
        _failed_downloads.setdefault(chat_id, {})[message_id] = {
            "file_name": os.path.basename(file_name) if file_name else "",
            "time": int(time.time()),
            "reason": reason,
        }
        _paused_tasks.discard(_task_key(chat_id, message_id))


def mark_download_retried(chat_id, message_id: int) -> None:
    """Forget one failure after it was queued again."""
    chat_id = normalize_chat_id(chat_id)
    with _PROGRESS_LOCK:
        messages = _failed_downloads.get(chat_id)
        if not messages:
            return
        messages.pop(message_id, None)
        if not messages:
            _failed_downloads.pop(chat_id, None)


def clear_failed_downloads(chat_id=None) -> int:
    """Forget every recorded failure, optionally for a single chat."""
    if chat_id is not None:
        chat_id = normalize_chat_id(chat_id)
    with _PROGRESS_LOCK:
        if chat_id is None:
            count = sum(len(messages) for messages in _failed_downloads.values())
            _failed_downloads.clear()
            return count

        return len(_failed_downloads.pop(chat_id, {}))


def mark_download_completed(
    chat_id,
    message_id: int,
    file_name: str = "",
    total_size: int = 0,
    upload_state: Optional[str] = None,
) -> None:
    """Move a finished task out of "downloading" and into the completed list.

    ``upload_state`` is ``"uploaded"`` when bytes were actually transferred to
    the cloud, ``"exists"`` when the remote already had an identical file, and
    ``None`` when the file stayed local only.

    Before this existed the row stayed in ``_download_result`` forever with its
    last reported percentage. The bot card said "success" while the Web UI kept
    showing the same message at 100% under "downloading".
    """
    chat_id = normalize_chat_id(chat_id)
    with _PROGRESS_LOCK:
        messages = _download_result.get(chat_id)
        row = messages.pop(message_id, None) if messages else None
        if messages is not None and not messages:
            _download_result.pop(chat_id, None)

        key = _task_key(chat_id, message_id)
        _paused_tasks.discard(key)
        _cancelled_tasks.discard(key)

        now = int(time.time())
        row = dict(row or {})
        row.update(
            {
                "chat": chat_id,
                "id": message_id,
                "file_name": row.get("file_name") or file_name,
                "total_size": row.get("total_size") or total_size,
                "down_byte": row.get("total_size") or total_size,
                "download_speed": 0,
                "completed_time": now,
                "upload_state": upload_state,
                "upload_success": bool(upload_state),
            }
        )
        _completed_downloads.pop(key, None)
        _completed_downloads[key] = row
        while len(_completed_downloads) > MAX_COMPLETED_DOWNLOADS:
            _completed_downloads.popitem(last=False)


def remove_download_result(chat_id, message_id: int) -> None:
    """Drop a row from the in-progress list."""
    chat_id = normalize_chat_id(chat_id)
    with _PROGRESS_LOCK:
        messages = _download_result.get(chat_id)
        if not messages or message_id not in messages:
            return
        messages.pop(message_id, None)
        if not messages:
            _download_result.pop(chat_id, None)


def delete_task(chat_id, message_id: int) -> bool:
    """Remove a task from every list and abort it if it is still downloading.

    This backs the Web UI "delete" action on all three tabs.
    """
    chat_id = normalize_chat_id(chat_id)
    key = _task_key(chat_id, message_id)
    with _PROGRESS_LOCK:
        existed = False
        downloading = False

        messages = _download_result.get(chat_id)
        if messages and message_id in messages:
            messages.pop(message_id, None)
            if not messages:
                _download_result.pop(chat_id, None)
            existed = True
            downloading = True

        if _completed_downloads.pop(key, None) is not None:
            existed = True

        failed = _failed_downloads.get(chat_id)
        if failed and message_id in failed:
            failed.pop(message_id, None)
            if not failed:
                _failed_downloads.pop(chat_id, None)
            existed = True

        _paused_tasks.discard(key)
        if downloading:
            # The progress callback is what actually unwinds the transfer: it
            # raises TaskCancelled for this single message.
            _cancelled_tasks.add(key)

        return existed


def set_task_paused(chat_id, message_id: int, paused: bool) -> bool:
    """Pause or resume a single message. Returns whether the state changed."""
    key = _task_key(chat_id, message_id)
    with _PROGRESS_LOCK:
        if paused:
            if key in _paused_tasks:
                return False
            _paused_tasks.add(key)
            return True

        if key not in _paused_tasks:
            return False
        _paused_tasks.discard(key)
        return True


def is_task_paused(chat_id, message_id: int) -> bool:
    """Whether a single message is paused."""
    with _PROGRESS_LOCK:
        return _task_key(chat_id, message_id) in _paused_tasks


def is_task_cancelled(chat_id, message_id: int) -> bool:
    """Whether the Web UI asked to delete a message that is still downloading."""
    with _PROGRESS_LOCK:
        return _task_key(chat_id, message_id) in _cancelled_tasks


def clear_task_cancelled(chat_id, message_id: int) -> None:
    """Forget the cancel flag once the task finalizer handled it."""
    with _PROGRESS_LOCK:
        _cancelled_tasks.discard(_task_key(chat_id, message_id))


def get_total_download_speed() -> int:
    """Sum of the per-file speeds the download table is showing.

    A separate global counter (bytes over wall time, reset by whichever progress
    callback happened to run last) drifted from the rows: on a throttled link a
    single chunk can take 10+ seconds, so the counter was stale for most of the
    time and the header read ``0 B/s`` while the table showed tens of KB/s.
    Summing the values the table renders keeps the two in agreement, and an
    empty table sums to zero by construction.
    """
    with _PROGRESS_LOCK:
        total = sum(
            value.get("download_speed") or 0
            for messages in _download_result.values()
            for value in messages.values()
        )
    return max(int(total), 0)


def get_download_state() -> DownloadState:
    """get download state"""
    return _download_state


# pylint: disable = W0603
def set_download_state(state: DownloadState):
    """set download state"""
    global _download_state
    _download_state = state


async def _wait_until_runnable(node: TaskNode, client: Client, message_id: int):
    """Block inside the progress callback while the task may not transfer.

    Waiting here (rather than skipping the callback) is what actually pauses the
    transfer: Pyrogram awaits the callback inside its read loop.
    """
    chat_id = node.chat_id
    while True:
        if node.is_stop_transmission:
            # Let Pyrogram abort the transfer; waiting here would keep the
            # worker parked forever after /stop.
            client.stop_transmission()
            return

        if is_task_cancelled(chat_id, message_id):
            raise TaskCancelled(f"message {message_id} deleted")

        if (
            get_download_state() is DownloadState.Downloading
            and not is_task_paused(chat_id, message_id)
        ):
            return

        await asyncio.sleep(0.5)


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

    await _wait_until_runnable(node, client, message_id)

    chat_id = normalize_chat_id(node.chat_id)

    with _PROGRESS_LOCK:
        messages = _download_result.setdefault(chat_id, {})
        if message_id in messages:
            value = messages[message_id]
            last_time = value["end_time"]
            download_speed = value["download_speed"]
            each_second_total_download = value["each_second_total_download"]
            end_time = value["end_time"]

            each_second_total_download += down_byte - value["down_byte"]

            if cur_time - last_time >= 1.0:
                download_speed = int(
                    each_second_total_download / (cur_time - last_time)
                )
                end_time = cur_time
                each_second_total_download = 0

            value["down_byte"] = down_byte
            value["end_time"] = end_time
            value["download_speed"] = max(download_speed, 0)
            value["each_second_total_download"] = each_second_total_download
            value["paused"] = is_task_paused(chat_id, message_id)
        else:
            messages[message_id] = {
                "down_byte": down_byte,
                "total_size": total_size,
                "file_name": file_name,
                "start_time": start_time,
                "end_time": cur_time,
                "download_speed": down_byte / max(cur_time - start_time, 1e-6),
                "each_second_total_download": down_byte,
                "task_id": node.task_id,
                "paused": is_task_paused(chat_id, message_id),
            }


def get_download_progress(chat_id, message_id: int) -> Optional[dict]:
    """Return one in-progress row (already copied)."""
    chat_id = normalize_chat_id(chat_id)
    with _PROGRESS_LOCK:
        messages = _download_result.get(chat_id)
        if not messages or message_id not in messages:
            return None
        return dict(messages[message_id])
