"""In-flight cloud-drive upload progress for the Web UI.

The rclone upload callback already feeds the bot progress card through
``TaskNode.cloud_drive_upload_stat_dict``, but the Flask thread has no access
to those objects. Every callback therefore also mirrors its numbers here, so
the Web UI can render an "Uploading" tab and a live upload speed.

The store is tiny (one row per file currently uploading) and lock guarded:
rclone calls the callback from a worker thread while the Web UI reads it.
"""

import threading
import time
from typing import Dict

from utils.format import parse_byte_size

_LOCK = threading.RLock()
#: chat key -> {message id: row}; rows are plain dicts so JSON needs no adapter.
_uploading: Dict[str, Dict[int, dict]] = {}


def _percent(value) -> float:
    """Parse rclone's ``"42%"`` (or ``"-"``) into a number."""
    text = str(value or "").strip().rstrip("%").strip()
    try:
        return float(text)
    except ValueError:
        return 0.0


def update(
    chat_id,
    message_id: int,
    file_name: str = "",
    transferred: str = "",
    total: str = "",
    percentage: str = "",
    speed: str = "",
    eta: str = "",
) -> None:
    """Record the last rclone stats line for one message."""
    percent = _percent(percentage)
    row = {
        "chat": chat_id,
        "id": int(message_id),
        "file_name": str(file_name or ""),
        "transferred": str(transferred or ""),
        "total": str(total or ""),
        "percentage": round(percent, 1),
        "progress": percent,
        "speed": str(speed or ""),
        "speed_bytes": parse_byte_size(speed),
        "eta": str(eta or ""),
        "updated_at": time.time(),
    }
    with _LOCK:
        _uploading.setdefault(str(chat_id), {})[int(message_id)] = row


def remove(chat_id, message_id: int) -> None:
    """Drop one message once its upload finished or failed."""
    with _LOCK:
        messages = _uploading.get(str(chat_id))
        if not messages:
            return
        messages.pop(int(message_id), None)
        if not messages:
            _uploading.pop(str(chat_id), None)


def snapshot() -> dict:
    """Copy of every upload row, grouped by chat (safe to serialise)."""
    with _LOCK:
        return {
            chat_key: {message_id: dict(row) for message_id, row in messages.items()}
            for chat_key, messages in _uploading.items()
        }


def rows() -> list:
    """Flat, newest-chat-first list of upload rows."""
    with _LOCK:
        flattened = [
            dict(row) for messages in _uploading.values() for row in messages.values()
        ]
    return sorted(flattened, key=lambda row: row.get("updated_at") or 0, reverse=True)


def count() -> int:
    """Number of files currently uploading."""
    with _LOCK:
        return sum(len(messages) for messages in _uploading.values())


def total_speed() -> int:
    """Sum of the per-file upload speeds in bytes/s."""
    with _LOCK:
        return sum(
            int(row.get("speed_bytes") or 0)
            for messages in _uploading.values()
            for row in messages.values()
        )


def clear() -> None:
    """Drop every row (used by shutdown and tests)."""
    with _LOCK:
        _uploading.clear()
