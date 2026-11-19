"""Download stat and persistent download history store.

Progress state is written from the asyncio loop (Pyrogram progress callbacks)
and read from the Flask thread (the Web UI). Every mutation and snapshot goes
through ``_PROGRESS_LOCK``: iterating a dict while the other thread inserts keys
raised ``RuntimeError: dictionary changed size during iteration`` and returned
500s during normal downloads.

Completed and failed records are stored in SQLite (``:memory:`` by default
during tests, or an on-disk WAL database initialized via :func:`init_history_db`
at startup) with no fixed row cap, supporting fast server-side filtering and
pagination as well as persistence across process restarts.

The module also owns the per-message controls the Web UI exposes:
``pause``/``resume`` (the progress callback waits) and ``cancel`` (the progress
callback raises, which aborts that single file instead of the whole client).
"""

import asyncio
import os
import sqlite3
import threading
import time
from enum import Enum
from typing import Any, List, Optional

from loguru import logger
from pyrogram import Client

from module.app import TaskNode

MAX_PAGE_SIZE = 500
DEFAULT_PAGE_SIZE = 25


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


_PROGRESS_LOCK = threading.RLock()
_download_result: dict = {}
_paused_tasks: set = set()
_cancelled_tasks: set = set()
_download_state: DownloadState = DownloadState.Downloading

_db_path: Optional[str] = None
_db_conn: Optional[sqlite3.Connection] = None

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS completed_downloads (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_key TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    file_name TEXT NOT NULL DEFAULT '',
    total_size INTEGER NOT NULL DEFAULT 0,
    completed_time INTEGER NOT NULL DEFAULT 0,
    upload_state TEXT,
    upload_success INTEGER NOT NULL DEFAULT 0,
    UNIQUE(chat_key, message_id)
);
CREATE INDEX IF NOT EXISTS idx_completed_upload_seq
    ON completed_downloads(upload_state, seq DESC);

CREATE TABLE IF NOT EXISTS failed_downloads (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_key TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    file_name TEXT NOT NULL DEFAULT '',
    failed_time INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    UNIQUE(chat_key, message_id)
);
CREATE INDEX IF NOT EXISTS idx_failed_time_seq
    ON failed_downloads(failed_time DESC, seq DESC);
"""


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


def _like_pattern(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _basename(file_name: str) -> str:
    if not file_name:
        return ""
    return str(file_name).replace("\\", "/").rsplit("/", 1)[-1]


def _get_db() -> sqlite3.Connection:
    """Return the active SQLite connection (must hold ``_PROGRESS_LOCK``)."""
    global _db_conn  # pylint: disable = W0603
    if _db_conn is not None:
        return _db_conn

    target = _db_path or ":memory:"
    if target != ":memory:":
        parent = os.path.dirname(os.path.abspath(target))
        if parent:
            os.makedirs(parent, exist_ok=True)

    conn = sqlite3.connect(target, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if target != ":memory:":
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass
    conn.executescript(_SCHEMA_SQL)
    conn.commit()
    _db_conn = conn
    return _db_conn


def init_history_db(db_path: Optional[str] = None) -> str:
    """Initialize (or reopen) the SQLite history store at ``db_path``.

    A database that cannot be opened (read-only volume, missing permissions)
    must not take the downloader down: fall back to an in-memory store and keep
    running, only losing history across restarts.
    """
    global _db_path, _db_conn  # pylint: disable = W0603
    with _PROGRESS_LOCK:
        if _db_conn is not None:
            try:
                _db_conn.close()
            except sqlite3.Error:
                pass
            _db_conn = None
        _db_path = db_path
        try:
            _get_db()
        except (sqlite3.Error, OSError) as error:
            logger.warning(
                f"history database {db_path} is not writable ({error}); "
                f"falling back to an in-memory store"
            )
            _db_path = None
            _db_conn = None
            _get_db()
        return _db_path or ":memory:"


def close_history_db() -> None:
    """Close the active SQLite connection cleanly."""
    global _db_conn  # pylint: disable = W0603
    with _PROGRESS_LOCK:
        if _db_conn is not None:
            try:
                _db_conn.close()
            except sqlite3.Error:
                pass
            _db_conn = None


def get_history_db_path() -> Optional[str]:
    """Return the configured SQLite history path (``None`` means in-memory)."""
    return _db_path


def _completed_dict(row: sqlite3.Row) -> dict:
    total_size = int(row["total_size"] or 0)
    return {
        "chat": normalize_chat_id(row["chat_key"]),
        "id": int(row["message_id"]),
        "file_name": row["file_name"] or "",
        "total_size": total_size,
        "down_byte": total_size,
        "download_speed": 0,
        "completed_time": int(row["completed_time"] or 0),
        "upload_state": row["upload_state"],
        "upload_success": bool(row["upload_success"]),
    }


def _failed_entry(row: sqlite3.Row) -> dict:
    return {
        "file_name": row["file_name"] or "",
        "time": int(row["failed_time"] or 0),
        "reason": row["reason"] or "",
    }


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


def count_downloading_downloads() -> int:
    """Return the number of in-flight download tasks."""
    with _PROGRESS_LOCK:
        return sum(len(messages) for messages in _download_result.values())


def get_completed_downloads() -> list:
    """Return all finished downloads, newest first."""
    with _PROGRESS_LOCK:
        conn = _get_db()
        rows = conn.execute(
            "SELECT chat_key, message_id, file_name, total_size, completed_time, "
            "upload_state, upload_success "
            "FROM completed_downloads ORDER BY seq DESC"
        ).fetchall()
        return [_completed_dict(row) for row in rows]


def count_completed_downloads() -> int:
    """Return total count of finished downloads."""
    with _PROGRESS_LOCK:
        conn = _get_db()
        row = conn.execute("SELECT COUNT(*) FROM completed_downloads").fetchone()
        return int(row[0]) if row else 0


def get_completed_download(chat_id, message_id: int) -> Optional[dict]:
    """Return one completed record (used by the "re-upload" action)."""
    chat_text = str(normalize_chat_id(chat_id))
    with _PROGRESS_LOCK:
        conn = _get_db()
        row = conn.execute(
            "SELECT chat_key, message_id, file_name, total_size, completed_time, "
            "upload_state, upload_success "
            "FROM completed_downloads WHERE chat_key = ? AND message_id = ?",
            (chat_text, int(message_id)),
        ).fetchone()
        return _completed_dict(row) if row else None


def update_completed_upload_state(
    chat_id, message_id: int, upload_state: Optional[str]
) -> bool:
    """Record the outcome of a re-upload (``uploaded`` / ``exists`` / ``None``).

    ``None`` keeps the row in "仅本地" so the button stays available.
    """
    chat_text = str(normalize_chat_id(chat_id))
    with _PROGRESS_LOCK:
        conn = _get_db()
        cursor = conn.execute(
            "UPDATE completed_downloads SET upload_state = ?, upload_success = ? "
            "WHERE chat_key = ? AND message_id = ?",
            (upload_state, 1 if upload_state else 0, chat_text, int(message_id)),
        )
        conn.commit()
        return bool(cursor.rowcount)


def query_completed_downloads(
    q: str = "",
    upload_state: str = "all",
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict:
    """Filter and paginate completed downloads on the server."""
    size = MAX_PAGE_SIZE if page_size <= 0 else min(int(page_size), MAX_PAGE_SIZE)
    where: List[str] = []
    params: List[Any] = []

    if upload_state == "local":
        where.append(
            "((upload_state IS NULL OR upload_state = '') AND upload_success = 0)"
        )
    elif upload_state == "uploaded":
        where.append(
            "(upload_state = 'uploaded' OR "
            "((upload_state IS NULL OR upload_state = '') AND upload_success = 1))"
        )
    elif upload_state == "exists":
        where.append("upload_state = 'exists'")

    query_text = (q or "").strip()
    if query_text:
        pat = _like_pattern(query_text)
        where.append(
            "(CAST(message_id AS TEXT) LIKE ? ESCAPE '\\' "
            "OR chat_key LIKE ? ESCAPE '\\' "
            "OR REPLACE(file_name, '\\', '/') LIKE ? ESCAPE '\\')"
        )
        params.extend([pat, pat, pat])

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    with _PROGRESS_LOCK:
        conn = _get_db()
        all_row = conn.execute("SELECT COUNT(*) FROM completed_downloads").fetchone()
        all_total = int(all_row[0]) if all_row else 0

        if where_sql:
            cnt_row = conn.execute(
                "SELECT COUNT(*) FROM completed_downloads" + where_sql, params
            ).fetchone()
            total = int(cnt_row[0]) if cnt_row else 0
        else:
            total = all_total

        pages = max(1, (total + size - 1) // size)
        cur_page = min(max(1, int(page)), pages)
        offset = (cur_page - 1) * size

        rows = conn.execute(
            "SELECT chat_key, message_id, file_name, total_size, completed_time, "
            "upload_state, upload_success "
            "FROM completed_downloads"
            + where_sql
            + " ORDER BY seq DESC LIMIT ? OFFSET ?",
            [*params, size, offset],
        ).fetchall()

        return {
            "rows": [_completed_dict(row) for row in rows],
            "total": total,
            "all_total": all_total,
            "page": cur_page,
            "page_size": size,
            "pages": pages,
        }


def get_failed_downloads() -> dict:
    """Return a snapshot of all failed downloads, grouped by chat id."""
    with _PROGRESS_LOCK:
        conn = _get_db()
        rows = conn.execute(
            "SELECT chat_key, message_id, file_name, failed_time, reason "
            "FROM failed_downloads ORDER BY seq ASC"
        ).fetchall()
        result: dict = {}
        for row in rows:
            chat_id = normalize_chat_id(row["chat_key"])
            result.setdefault(chat_id, {})[int(row["message_id"])] = _failed_entry(row)
        return result


def count_failed_downloads() -> int:
    """Return total count of failed downloads."""
    with _PROGRESS_LOCK:
        conn = _get_db()
        row = conn.execute("SELECT COUNT(*) FROM failed_downloads").fetchone()
        return int(row[0]) if row else 0


def query_failed_downloads(
    q: str = "",
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict:
    """Filter and paginate failed downloads on the server."""
    size = MAX_PAGE_SIZE if page_size <= 0 else min(int(page_size), MAX_PAGE_SIZE)
    where: List[str] = []
    params: List[Any] = []

    query_text = (q or "").strip()
    if query_text:
        pat = _like_pattern(query_text)
        where.append(
            "(CAST(message_id AS TEXT) LIKE ? ESCAPE '\\' "
            "OR chat_key LIKE ? ESCAPE '\\' "
            "OR file_name LIKE ? ESCAPE '\\' "
            "OR reason LIKE ? ESCAPE '\\')"
        )
        params.extend([pat, pat, pat, pat])

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    with _PROGRESS_LOCK:
        conn = _get_db()
        all_row = conn.execute("SELECT COUNT(*) FROM failed_downloads").fetchone()
        all_total = int(all_row[0]) if all_row else 0

        if where_sql:
            cnt_row = conn.execute(
                "SELECT COUNT(*) FROM failed_downloads" + where_sql, params
            ).fetchone()
            total = int(cnt_row[0]) if cnt_row else 0
        else:
            total = all_total

        pages = max(1, (total + size - 1) // size)
        cur_page = min(max(1, int(page)), pages)
        offset = (cur_page - 1) * size

        rows = conn.execute(
            "SELECT chat_key, message_id, file_name, failed_time, reason "
            "FROM failed_downloads"
            + where_sql
            + " ORDER BY failed_time DESC, seq DESC LIMIT ? OFFSET ?",
            [*params, size, offset],
        ).fetchall()

        formatted = [
            {
                "chat": normalize_chat_id(row["chat_key"]),
                "id": int(row["message_id"]),
                "filename": row["file_name"] or "",
                "time": int(row["failed_time"] or 0),
                "reason": row["reason"] or "",
            }
            for row in rows
        ]

        return {
            "rows": formatted,
            "total": total,
            "all_total": all_total,
            "page": cur_page,
            "page_size": size,
            "pages": pages,
        }


def mark_download_failed(
    chat_id, message_id: int, file_name: str = "", reason: str = ""
) -> None:
    """Remember a failed download so the Web UI can list and retry it."""
    chat_id = normalize_chat_id(chat_id)
    chat_key = str(chat_id)
    msg_id = int(message_id)
    now = int(time.time())
    base_name = _basename(file_name)
    with _PROGRESS_LOCK:
        conn = _get_db()
        conn.execute(
            "INSERT OR REPLACE INTO failed_downloads "
            "(chat_key, message_id, file_name, failed_time, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_key, msg_id, base_name, now, reason or ""),
        )
        conn.commit()
        _paused_tasks.discard(_task_key(chat_id, msg_id))


def mark_download_retried(chat_id, message_id: int) -> None:
    """Forget one failure after it was queued again."""
    chat_id = normalize_chat_id(chat_id)
    chat_key = str(chat_id)
    msg_id = int(message_id)
    with _PROGRESS_LOCK:
        conn = _get_db()
        conn.execute(
            "DELETE FROM failed_downloads WHERE chat_key = ? AND message_id = ?",
            (chat_key, msg_id),
        )
        conn.commit()


def clear_failed_downloads(chat_id=None) -> int:
    """Forget every recorded failure, optionally for a single chat."""
    with _PROGRESS_LOCK:
        conn = _get_db()
        if chat_id is None:
            cur = conn.execute("DELETE FROM failed_downloads")
        else:
            chat_key = str(normalize_chat_id(chat_id))
            cur = conn.execute(
                "DELETE FROM failed_downloads WHERE chat_key = ?", (chat_key,)
            )
        conn.commit()
        return cur.rowcount or 0


def clear_completed_downloads(chat_id=None) -> int:
    """Forget every completed download record (does not delete local files)."""
    with _PROGRESS_LOCK:
        conn = _get_db()
        if chat_id is None:
            cur = conn.execute("DELETE FROM completed_downloads")
        else:
            chat_key = str(normalize_chat_id(chat_id))
            cur = conn.execute(
                "DELETE FROM completed_downloads WHERE chat_key = ?", (chat_key,)
            )
        conn.commit()
        return cur.rowcount or 0


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
    """
    chat_id = normalize_chat_id(chat_id)
    chat_key = str(chat_id)
    msg_id = int(message_id)
    with _PROGRESS_LOCK:
        messages = _download_result.get(chat_id)
        row = messages.pop(msg_id, None) if messages else None
        if messages is not None and not messages:
            _download_result.pop(chat_id, None)

        key = _task_key(chat_id, msg_id)
        _paused_tasks.discard(key)
        _cancelled_tasks.discard(key)

        now = int(time.time())
        row = dict(row or {})
        final_name = row.get("file_name") or file_name or ""
        final_size = int(row.get("total_size") or total_size or 0)
        upload_success = 1 if upload_state else 0

        conn = _get_db()
        conn.execute(
            "INSERT OR REPLACE INTO completed_downloads "
            "(chat_key, message_id, file_name, total_size, completed_time, "
            "upload_state, upload_success) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                chat_key,
                msg_id,
                final_name,
                final_size,
                now,
                upload_state,
                upload_success,
            ),
        )
        # If this message previously failed and succeeded on retry, clear its failure.
        conn.execute(
            "DELETE FROM failed_downloads WHERE chat_key = ? AND message_id = ?",
            (chat_key, msg_id),
        )
        conn.commit()


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
    chat_key = str(chat_id)
    msg_id = int(message_id)
    key = _task_key(chat_id, msg_id)
    with _PROGRESS_LOCK:
        existed = False
        downloading = False

        messages = _download_result.get(chat_id)
        if messages and msg_id in messages:
            messages.pop(msg_id, None)
            if not messages:
                _download_result.pop(chat_id, None)
            existed = True
            downloading = True

        conn = _get_db()
        c_cur = conn.execute(
            "DELETE FROM completed_downloads WHERE chat_key = ? AND message_id = ?",
            (chat_key, msg_id),
        )
        if c_cur.rowcount:
            existed = True

        f_cur = conn.execute(
            "DELETE FROM failed_downloads WHERE chat_key = ? AND message_id = ?",
            (chat_key, msg_id),
        )
        if f_cur.rowcount:
            existed = True

        if c_cur.rowcount or f_cur.rowcount:
            conn.commit()

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
    """Sum of the per-file speeds the download table is showing."""
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
