"""New-release detection for the bot help card and the Web UI.

GitHub's unauthenticated API is rate limited and the container may not reach
github.com at all (mirrors, offline installs), so the check is best effort: it
runs in a daemon thread at most once per :data:`TTL` seconds, keeps the last
answer in memory, and never raises into the caller. Users can opt out with
``TDL_VERSION_CHECK=0``.
"""

import http.client
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from typing import Dict, Optional, Tuple

from loguru import logger

import utils

#: Where the latest release is read from and where users are sent to.
RELEASES_API = "https://api.github.com/repos/QCEnjoyLL/tg-dl/releases/latest"
RELEASES_URL = "https://github.com/QCEnjoyLL/tg-dl/releases"

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
_proxy: Optional[dict] = None
_thread_started = False


def set_proxy(proxy: Optional[dict]) -> None:
    """Store the optional Telegram/app proxy config for version checks."""
    global _proxy  # pylint: disable = W0603
    with _LOCK:
        _proxy = dict(proxy) if isinstance(proxy, dict) and proxy else None


def _redact_secrets(text: str) -> str:
    """Mask URL userinfo credentials before saving or logging an error."""
    return re.sub(r"(://)[^/@\s]+@", r"\1***@", str(text or ""))


def _open_request(request: urllib.request.Request, proxy: Optional[dict]):
    """Open ``request``, routing via ``proxy`` when configured."""
    if not isinstance(proxy, dict) or not proxy.get("hostname") or not proxy.get("port"):
        return urllib.request.urlopen(request, timeout=TIMEOUT)

    scheme = str(proxy.get("scheme") or "http").strip().lower()
    hostname = str(proxy["hostname"]).strip()
    port = int(proxy["port"])
    username = str(proxy.get("username") or "")
    password = str(proxy.get("password") or "")

    if scheme in ("http", "https"):
        auth = ""
        if username or password:
            user_enc = urllib.parse.quote(username, safe="")
            pass_enc = urllib.parse.quote(password, safe="")
            auth = f"{user_enc}:{pass_enc}@"
        host_fmt = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
        proxy_url = f"{scheme}://{auth}{host_fmt}:{port}"
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        )
        return opener.open(request, timeout=TIMEOUT)

    if scheme in ("socks4", "socks5", "socks5h"):
        import socks  # pylint: disable = C0415

        proxy_type = socks.SOCKS4 if scheme == "socks4" else socks.SOCKS5
        rdns = scheme != "socks4"

        class _SocksHTTPSConnection(http.client.HTTPSConnection):
            def connect(self):
                raw_sock = socks.create_connection(
                    (self.host, self.port),
                    timeout=self.timeout,
                    source_address=self.source_address,
                    proxy_type=proxy_type,
                    proxy_addr=hostname,
                    proxy_port=port,
                    proxy_rdns=rdns,
                    proxy_username=username or None,
                    proxy_password=password or None,
                )
                server_hostname = (
                    self._tunnel_host
                    if getattr(self, "_tunnel_host", None)
                    else self.host
                )
                self.sock = self._context.wrap_socket(
                    raw_sock, server_hostname=server_hostname
                )

        class _SocksHTTPSHandler(urllib.request.HTTPSHandler):
            def https_open(self, req):
                return self.do_open(
                    _SocksHTTPSConnection, req, context=self._context
                )

        opener = urllib.request.build_opener(_SocksHTTPSHandler())
        return opener.open(request, timeout=TIMEOUT)

    return urllib.request.urlopen(request, timeout=TIMEOUT)


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
    with _LOCK:
        proxy = dict(_proxy) if _proxy else None
    with _open_request(request, proxy) as response:
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
        safe_err = _redact_secrets(str(error))
        with _LOCK:
            _last_error = safe_err
        logger.warning(f"version check failed: {safe_err}")


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


def start_background_check(
    interval: int = TTL, proxy: Optional[dict] = None
) -> None:
    """Start the daemon refresher; repeated calls are ignored."""
    global _thread_started  # pylint: disable = W0603
    if proxy is not None:
        set_proxy(proxy)
    if not version_check_enabled() or _thread_started:
        return
    _thread_started = True

    def loop() -> None:
        while True:
            refresh(force=True)
            _WAKE.wait(interval)

    threading.Thread(target=loop, name="version-check", daemon=True).start()
