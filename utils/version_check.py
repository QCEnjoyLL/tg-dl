"""New-release detection for the bot help card and the Web UI.

GitHub's unauthenticated API is rate limited and the container may not reach
github.com at all (mirrors, offline installs), so the check is best effort: it
runs in a daemon thread at most once per :data:`TTL` seconds, keeps the last
answer in memory, and never raises into the caller. Users can opt out with
``TDL_VERSION_CHECK=0``.
"""

import json
import os
import re
import threading
import time
import urllib.request
from typing import Dict, Optional, Tuple

from loguru import logger

import utils

#: Where the latest release is read from and where users are sent to.
RELEASES_API = "https://api.github.com/repos/QCEnjoyLL/tg-dl/releases/latest"
RELEASES_URL = "https://github.com/QCEnjoyLL/tg-dl/releases"

#: Minimum seconds between two GitHub requests.
TTL = 6 * 60 * 60

#: Minimum seconds between two GitHub requests. 15 minutes keeps a fresh
#: release visible shortly after it is published while staying far below the
#: unauthenticated API limit (60 requests/hour per IP).
TTL = 15 * 60

#: Never block a bot reply or a web request for long on a dead network.
TIMEOUT = 6.0

_LOCK = threading.RLock()
_WAKE = threading.Event()
_checked_at = 0.0
_latest: Optional[str] = None
_url = RELEASES_URL
_last_error: Optional[str] = None
_thread_started = False


def version_check_enabled() -> bool:
    """Honour the ``TDL_VERSION_CHECK`` kill switch."""
    value = os.environ.get("TDL_VERSION_CHECK", "1").strip().lower()
    return value not in ("0", "false", "no", "off", "")


def parse_version(value) -> Tuple[int, ...]:
    """Turn ``v2.13.2`` into ``(2, 13, 2)`` so releases can be ordered."""
    numbers = re.findall(r"\d+", str(value or ""))
    return tuple(int(part) for part in numbers[:3]) or (0,)


def is_newer(latest, current) -> bool:
    """True when ``latest`` is a later release than ``current``."""
    return bool(latest) and parse_version(latest) > parse_version(current)


def _fetch() -> None:
    """One GitHub request; store the answer, log only real updates."""
    global _checked_at, _latest, _url, _last_error  # pylint: disable = W0603

    request = urllib.request.Request(
        RELEASES_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "tg-dl-version-check",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8"))
    tag = str(payload.get("tag_name") or "").strip().lstrip("vV")
    if not tag:
        raise ValueError("release payload has no tag_name")

    with _LOCK:
        _checked_at = time.time()
        _latest = tag
        _url = str(payload.get("html_url") or RELEASES_URL)
        _last_error = None
    if is_newer(tag, utils.__version__):
        logger.info(f"new version available: v{tag} (current v{utils.__version__})")


def refresh(force: bool = False) -> None:
    """Query GitHub unless the last answer is still fresh."""
    global _last_error  # pylint: disable = W0603

    if not version_check_enabled():
        return
    with _LOCK:
        fresh = _checked_at and (time.time() - _checked_at) < TTL
    if fresh and not force:
        return
    try:
        _fetch()
    except Exception as error:  # pylint: disable = W0703
        # Offline, rate limited, DNS blocked: keep the previous answer but make
        # the failure visible in the log and in /get_latest_version.
        with _LOCK:
            _last_error = str(error)
        logger.warning(f"version check failed: {error}")


def update_info() -> Dict[str, object]:
    """Snapshot for the bot help card and the Web UI (no network I/O)."""
    with _LOCK:
        latest = _latest
        checked_at = _checked_at
        url = _url
        last_error = _last_error
    current = utils.__version__
    return {
        "current": current,
        "latest": latest or "",
        "newer": is_newer(latest, current),
        "url": url,
        "checked_at": int(checked_at),
        "error": last_error or "",
    }


def start_background_check(interval: int = TTL) -> None:
    """Start the daemon refresher; repeated calls are ignored."""
    global _thread_started  # pylint: disable = W0603
    if not version_check_enabled() or _thread_started:
        return
    _thread_started = True

    def loop() -> None:
        while True:
            refresh(force=True)
            _WAKE.wait(interval)

    threading.Thread(target=loop, name="version-check", daemon=True).start()
