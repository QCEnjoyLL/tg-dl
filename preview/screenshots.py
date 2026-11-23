"""Regenerate ``docs/ui-*.png`` from the static preview (no app server needed).

The preview pages are the mock built by ``preview/build.py``, so the screenshots
need neither a Telegram session nor a running downloader. Chrome's command line
``--window-size`` clamps small windows (a "390px phone" really rendered at
512px and got cropped), so this drives Chrome through the DevTools protocol and
uses ``Emulation.setDeviceMetricsOverride`` for a true mobile viewport.

Usage (``preview/screenshots.ps1`` is a thin wrapper):

    python preview/screenshots.py --chrome "C:/Program Files/Google/Chrome/Application/chrome.exe"

Only the Python standard library is used; Chrome/Edge must be installed.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREVIEW = ROOT / "preview"
DOCS = ROOT / "docs"

#: width, height and page for every README screenshot (keep in sync with README).
SHOTS = [
    ("ui-light.png", 1440, 900, "preview/index.html?theme=light"),
    ("ui-dark.png", 1440, 900, "preview/index.html?theme=dark"),
    ("ui-config.png", 1440, 900, "preview/index-config.html?theme=light"),
    ("ui-failed.png", 1440, 900, "preview/index.html?theme=light#failed"),
    ("ui-login.png", 900, 760, "preview/login.html?theme=light"),
    ("ui-mobile.png", 390, 760, "preview/index.html?theme=light"),
]

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def find_chrome(explicit: str = "") -> str:
    """Locate a Chrome/Edge binary, honouring an explicit path first."""
    if explicit:
        if not Path(explicit).exists():
            raise SystemExit(f"chrome not found: {explicit}")
        return explicit
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise SystemExit("Chrome/Edge not found; pass --chrome <path>")


class _CDP:
    """Tiny DevTools-protocol client: one websocket, one page target."""

    def __init__(self, ws_url: str):
        from urllib.parse import urlparse

        parsed = urlparse(ws_url)
        self._sock = socket.create_connection(
            (parsed.hostname, parsed.port or 80), timeout=30
        )
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {parsed.path or '/'} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(request.encode())
        self._buffer = b""
        while b"\r\n\r\n" not in self._buffer:
            self._buffer += self._sock.recv(8192)
        handshake, _, self._buffer = self._buffer.partition(b"\r\n\r\n")
        if b" 101 " not in handshake.split(b"\r\n")[0]:
            raise RuntimeError(f"websocket upgrade failed: {handshake[:120]!r}")
        self._next_id = 0

    def _read_exact(self, size: int) -> bytes:
        while len(self._buffer) < size:
            chunk = self._sock.recv(max(size - len(self._buffer), 4096))
            if not chunk:
                raise RuntimeError("websocket closed")
            self._buffer += chunk
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data

    def _send_frame(self, payload: bytes, opcode: int = 1) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += length.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += length.to_bytes(8, "big")
        mask = os.urandom(4)
        header += mask
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def _recv_frame(self) -> tuple[int, bytes]:
        first, second = self._read_exact(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = int.from_bytes(self._read_exact(2), "big")
        elif length == 127:
            length = int.from_bytes(self._read_exact(8), "big")
        if second & 0x80:  # masked server frames are not expected
            self._read_exact(4)
        return opcode, self._read_exact(length)

    def send(self, method: str, params: dict | None = None):
        self._next_id += 1
        message_id = self._next_id
        payload = json.dumps(
            {"id": message_id, "method": method, "params": params or {}}
        ).encode()
        self._send_frame(payload)
        while True:
            opcode, data = self._recv_frame()
            if opcode == 9:  # ping
                self._send_frame(data, opcode=10)
                continue
            if opcode not in (1, 2):
                continue
            try:
                reply = json.loads(data)
            except ValueError:
                continue
            if reply.get("id") != message_id:
                continue
            if "error" in reply:
                raise RuntimeError(f"{method} failed: {reply['error']}")
            return reply.get("result", {})

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _QuietHandler(SimpleHTTPRequestHandler):
    """Static handler without access logs and without shutdown tracebacks."""

    def log_message(self, *args):  # noqa: D102 - silence per-request logging
        return

    def handle_one_request(self):  # noqa: D102 - Chrome resets on shutdown
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True


class _QuietServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that ignores client disconnects."""

    def handle_error(self, request, client_address):  # noqa: D102
        return


def _serve() -> tuple[ThreadingHTTPServer, threading.Thread]:
    handler = lambda *args, **kwargs: _QuietHandler(  # noqa: E731
        *args, directory=str(ROOT), **kwargs
    )
    server = _QuietServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _wait_for_page(port: int, process: subprocess.Popen, timeout: float = 20.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            raise SystemExit(f"chrome exited with {process.returncode}")
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/list", timeout=2
            ) as response:
                targets = json.loads(response.read().decode())
            for target in targets:
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return target["webSocketDebuggerUrl"]
        except OSError:
            pass
        time.sleep(0.25)
    raise SystemExit("chrome did not expose a page target")


def _launch_chrome(
    chrome: str, profile: str, port: int, no_sandbox: bool
) -> subprocess.Popen:
    args = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        "--remote-allow-origins=*",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "about:blank",
    ]
    if no_sandbox:
        # Chrome's sandbox cannot always start inside containers / CI jobs or
        # when launched from another process (it dies with STATUS_BREAKPOINT);
        # retried only after the sandboxed launch fails.
        args.insert(1, "--no-sandbox")
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _stop_chrome(process: subprocess.Popen, profile: str) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    shutil.rmtree(profile, ignore_errors=True)


def _build_preview() -> None:
    subprocess.run(
        [sys.executable, str(PREVIEW / "build.py")], cwd=str(ROOT), check=True
    )
    index = (PREVIEW / "index.html").read_text(encoding="utf-8")
    click = (
        "<script>window.addEventListener('load',function(){setTimeout(function(){"
        "var b=document.getElementById('config-btn');if(b){b.click();}},400);});"
        "</script>"
    )
    (PREVIEW / "index-config.html").write_text(
        index.replace("</body>", click + "</body>"), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome", default="", help="Chrome/Edge binary")
    parser.add_argument("--out", default=str(DOCS), help="output directory")
    parser.add_argument("--keep-chrome", action="store_true", help="debug helper")
    args = parser.parse_args()

    chrome = find_chrome(args.chrome)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    _build_preview()

    process = None
    profile = ""
    ws_url = ""
    for no_sandbox in (False, True):
        debug_port = _free_port()
        profile = tempfile.mkdtemp(prefix="tdl-shot-")
        process = _launch_chrome(chrome, profile, debug_port, no_sandbox)
        try:
            ws_url = _wait_for_page(debug_port, process)
            break
        except SystemExit as error:
            _stop_chrome(process, profile)
            process = None
            if no_sandbox:
                raise SystemExit(f"could not start chrome: {error}") from error
            print(f"chrome failed without --no-sandbox ({error}); retrying")

    server = None
    cdp = None
    try:
        server, _thread = _serve()
        origin = f"http://127.0.0.1:{server.server_port}/"
        cdp = _CDP(ws_url)
        cdp.send("Page.enable")
        for name, width, height, page in SHOTS:
            cdp.send(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": width,
                    "height": height,
                    "deviceScaleFactor": 1,
                    "mobile": width < 600,
                },
            )
            cdp.send("Page.navigate", {"url": origin + page})
            time.sleep(2.5)
            result = cdp.send("Page.captureScreenshot", {"format": "png"})
            (out_dir / name).write_bytes(base64.b64decode(result["data"]))
            print(f"wrote {out_dir / name}")
    finally:
        if cdp is not None:
            cdp.close()
        if server is not None:
            server.shutdown()
            server.server_close()
        if process is not None:
            if args.keep_chrome:
                print(f"chrome pid {process.pid} kept alive (profile {profile})")
            else:
                _stop_chrome(process, profile)
        (PREVIEW / "index-config.html").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
