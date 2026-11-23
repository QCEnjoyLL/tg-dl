"""web ui for media download"""

import hmac
import io
import logging
import os
import re
import secrets
import threading
import time
from concurrent import futures
from typing import Optional, Tuple, Union

from flask import Flask, jsonify, redirect, render_template, request
from flask_login import LoginManager, UserMixin, login_required, login_user, logout_user
from loguru import logger as app_log
from ruamel import yaml
from werkzeug.security import check_password_hash

import utils
from module import config_form, disk_guard, upload_stat
from module.app import Application
from module.download_stat import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    DownloadState,
    clear_completed_downloads,
    clear_failed_downloads,
    count_completed_downloads,
    count_downloading_downloads,
    count_failed_downloads,
    delete_task,
    get_completed_downloads,
    get_download_result,
    get_download_state,
    get_failed_downloads,
    get_total_download_speed,
    is_task_cancelled,
    is_task_paused,
    mark_download_retried,
    normalize_chat_id,
    query_completed_downloads,
    query_failed_downloads,
    set_download_state,
    set_task_paused,
)
from utils.atomic_write import write_text_atomic
from utils.format import format_byte
from utils import version_check

log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)

_yaml = yaml.YAML()

#: Config keys never sent to the browser. They are replaced by ``CONFIG_MASK``
#: and restored from the file when the form is saved unchanged.
SECRET_CONFIG_KEYS = ("api_hash", "bot_token", "web_login_secret", "password")
CONFIG_MASK = "********"
_SECRET_LINE = re.compile(
    r"^(\s*)(api_hash|bot_token|web_login_secret|password)(\s*:\s*)(.*?)\s*$"
)

_flask_app = Flask(__name__)

# The session cookie is the only thing protecting the Web UI when a password is
# configured, so the signing key must never be a public constant. A random key
# per process is fine: it invalidates old sessions on restart, which is
# preferable to forgeable cookies. Set TDL_WEB_SECRET_KEY to keep sessions
# across restarts behind a stable deployment.
_flask_app.config["SECRET_KEY"] = (
    os.environ.get("TDL_WEB_SECRET_KEY") or secrets.token_hex(32)
)
_flask_app.config["SESSION_COOKIE_HTTPONLY"] = True
_flask_app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
if os.environ.get("TDL_WEB_COOKIE_SECURE", "").lower() in ("1", "true", "yes"):
    _flask_app.config["SESSION_COOKIE_SECURE"] = True

_login_manager = LoginManager()
_login_manager.login_view = "login"
_login_manager.init_app(_flask_app)
web_login_users: dict = {}
_retry_handler = None
_upload_handler = None
_application: Optional[Application] = None


def set_retry_handler(handler):
    """Register the callback used by /retry_failed to re-queue a message."""
    global _retry_handler
    _retry_handler = handler


def set_upload_handler(handler):
    """Register the callback used by /retry_upload to upload one kept file."""
    global _upload_handler
    _upload_handler = handler


class User(UserMixin):
    """Web Login User"""

    def __init__(self):
        self.sid = "root"

    @property
    def id(self):
        """ID"""
        return self.sid


@_login_manager.user_loader
def load_user(_):
    """
    Load a user object from the user ID.

    Returns:
        User: The user object.
    """
    return User()


def get_flask_app() -> Flask:
    """get flask app instance"""
    return _flask_app


def run_web_server(app: Application, host: str):
    """
    Runs a web server using the Flask framework.
    """
    # debug=True installs Werkzeug's interactive debugger (/console), which is
    # remote code execution for anyone who can reach the port. Only allow it on
    # loopback, where the operator is the only client.
    loopback = host in ("127.0.0.1", "localhost", "::1")
    debug = bool(app.debug_web) and loopback
    if app.debug_web and not loopback:
        app_log.warning(
            "debug_web=true would expose Werkzeug's interactive debugger on {}; "
            "ignoring it. Bind to 127.0.0.1 if you really need the debugger.",
            host,
        )
    get_flask_app().run(host, app.web_port, debug=debug, use_reloader=False)


def _effective_web_host(app: Application) -> str:
    """Never expose an unauthenticated UI on a network interface.

    Upstream shipped ``web_login_secret`` commented out and still bound the
    Flask server to ``0.0.0.0``. That made a fresh install readable and
    controllable from the LAN. Without a password the Web UI is now forced to
    loopback so the operator has to opt in explicitly.
    """
    host = (app.web_host or "127.0.0.1").strip()
    if app.web_login_secret:
        return host

    if host not in ("127.0.0.1", "localhost", "::1"):
        app_log.warning(
            "web_login_secret is empty; forcing the Web UI to listen on "
            "127.0.0.1 instead of {}. Set web_login_secret to expose it.",
            host,
        )
        return "127.0.0.1"
    return host


# pylint: disable = W0603
def init_web(app: Application):
    """
    Set the value of the users variable.

    Args:
        users: The list of users to set.

    Returns:
        None.
    """
    global web_login_users
    global _application
    _application = app
    _refresh_login_config(app)

    host = _effective_web_host(app)
    if app.debug_web:
        # Non-daemon threads keep the process alive after main() returns, which
        # turns `docker stop` into a 2 minute wait followed by SIGKILL.
        threading.Thread(target=run_web_server, args=(app, host), daemon=True).start()
    else:
        threading.Thread(
            target=get_flask_app().run, daemon=True, args=(host, app.web_port)
        ).start()


def _refresh_login_config(app: Application):
    """Point the login form at the current ``web_login_secret``."""
    global web_login_users
    if app.web_login_secret:
        web_login_users = {"root": app.web_login_secret}
        _flask_app.config["LOGIN_DISABLED"] = False
    else:
        web_login_users = {}
        _flask_app.config["LOGIN_DISABLED"] = True


@_flask_app.after_request
def _set_security_headers(response):
    """Cheap, safe defaults; the UI never needs to be framed or MIME-sniffed."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-TDL-Version", utils.__version__)

    # A cached index.html served next to a cached app.js from another release is
    # how the UI ended up dead ("Cannot set properties of null"): the old script
    # looked for an element the new markup no longer has. Never store the pages,
    # and let the versioned static URLs be cached forever instead.
    if response.mimetype == "text/html":
        response.headers["Cache-Control"] = "no-store, max-age=0"
    elif request.endpoint == "static" and request.args.get("v"):
        # Only versioned URLs may be cached forever; an unversioned request keeps
        # Flask's normal revalidation.
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"

    return response


def _verify_password(secret: str, password: str) -> bool:
    """Compare a configured password, supporting werkzeug hashes as well."""
    if not secret:
        return False
    if secret.startswith(("pbkdf2:", "scrypt:")):
        try:
            return check_password_hash(secret, password)
        except (TypeError, ValueError):
            return False
    return hmac.compare_digest(str(secret).encode("utf-8"), password.encode("utf-8"))


@_flask_app.route("/login", methods=["GET", "POST"])
def login():
    """
    Function to handle the login route.

    Parameters:
    - No parameters

    Returns:
    - If the request method is "POST" and the username and
      password match the ones in the web_login_users dictionary,
      it returns a JSON response with a code of "1".
    - Otherwise, it returns a JSON response with a code of "0".
    - If the request method is not "POST", it returns the rendered "login.html" template.
    """
    if request.method == "POST":
        client_ip = request.remote_addr or "unknown"
        retry_after = _login_retry_after(client_ip)
        if retry_after > 0:
            app_log.warning(
                "login blocked for {}: too many failures ({:.0f}s left)",
                client_ip,
                retry_after,
            )
            return jsonify(
                {
                    "code": "0",
                    "msg": f"尝试次数过多，请 {int(retry_after) + 1} 秒后再试",
                }
            )

        username = "root"
        # TLS terminates in front of the app; the previous AES-in-JS layer used
        # a key shipped with the page and added no protection over plain HTTP.
        password = request.form.get("password", "")

        if not password:
            return jsonify({"code": "0"})

        secret = web_login_users.get(username)
        if secret and _verify_password(str(secret), password):
            _clear_login_failures(client_ip)
            user = User()
            login_user(user)
            return jsonify({"code": "1"})

        _register_login_failure(client_ip)
        return jsonify({"code": "0"})

    return render_template("login.html", version=utils.__version__)


#: A single shared password protects the whole Web UI, so unlimited guessing is
#: the cheapest attack once the UI is reachable from the network.
_LOGIN_MAX_FAILURES = 5
_LOGIN_LOCKOUT_SECONDS = 60.0
#: Failed attempts are forgotten after this long. Without it, every address that
#: ever guessed wrong once kept an entry forever, and a public scanner grew the
#: dict without bound.
_LOGIN_FAILURE_WINDOW_SECONDS = 15 * 60.0
#: Pruning scans the whole dict, so do it at most this often.
_LOGIN_PRUNE_INTERVAL_SECONDS = 60.0
#: ``ip -> (failures, last_attempt, locked_until)``
_login_failures: dict = {}
#: ``-inf`` means "never pruned yet", so the first failure always runs a sweep.
_login_last_prune = float("-inf")
_login_failures_lock = threading.Lock()


def _login_retry_after(client_ip: str) -> float:
    """Seconds the client has to wait before another attempt (0 = go ahead)."""
    now = time.monotonic()
    with _login_failures_lock:
        entry = _login_failures.get(client_ip)
        if not entry:
            return 0.0
        failures, _, locked_until = entry
        if failures < _LOGIN_MAX_FAILURES:
            return 0.0
        # The lockout expired: start a fresh count instead of re-locking on the
        # very next attempt.
        remaining = locked_until - now
        if remaining <= 0:
            _login_failures.pop(client_ip, None)
            return 0.0
        return remaining


def _prune_login_failures(now: float) -> None:
    """Forget attempts old enough that they cannot lock anyone out (lock held)."""
    stale = [
        client_ip
        for client_ip, (_, last_attempt, _) in _login_failures.items()
        if now - last_attempt >= _LOGIN_FAILURE_WINDOW_SECONDS
    ]
    for client_ip in stale:
        _login_failures.pop(client_ip, None)


def _register_login_failure(client_ip: str) -> None:
    """Count a failed attempt and start the lockout once the limit is hit."""
    global _login_last_prune  # pylint: disable = global-statement
    now = time.monotonic()
    with _login_failures_lock:
        if now - _login_last_prune >= _LOGIN_PRUNE_INTERVAL_SECONDS:
            _login_last_prune = now
            _prune_login_failures(now)
        failures = _login_failures.get(client_ip, (0, now, 0.0))[0] + 1
        locked_until = (
            now + _LOGIN_LOCKOUT_SECONDS if failures >= _LOGIN_MAX_FAILURES else 0.0
        )
        _login_failures[client_ip] = (failures, now, locked_until)


def _clear_login_failures(client_ip: str) -> None:
    with _login_failures_lock:
        _login_failures.pop(client_ip, None)


@_flask_app.route("/logout", methods=["POST", "GET"])
@login_required
def logout():
    """Drop the session and send the browser back to the login form."""
    logout_user()
    if request.method == "GET":
        return redirect("/login")
    return jsonify({"code": "1"})


@_flask_app.route("/")
@login_required
def index():
    """Index html"""
    return render_template(
        "index.html",
        download_state=(
            "pause" if get_download_state() is DownloadState.Downloading else "continue"
        ),
        version=utils.__version__,
        login_enabled=bool(web_login_users),
    )


@_flask_app.route("/get_download_status")
@login_required
def get_download_speed():
    """Get download speed and per-tab counts."""
    return jsonify(
        {
            "download_speed": format_byte(get_total_download_speed()) + "/s",
            "upload_speed": format_byte(upload_stat.total_speed()) + "/s",
            "state": (
                "running"
                if get_download_state() is DownloadState.Downloading
                else "paused"
            ),
            # Counts come from SQL so the tab badges stay right even though the
            # list itself is now paginated.
            "downloading": count_downloading_downloads(),
            "uploading": upload_stat.count(),
            "completed": count_completed_downloads(),
            "failed": count_failed_downloads(),
            # The "re-upload" button is pointless without an upload target.
            "upload_enabled": bool(
                _application is not None
                and _application.cloud_drive_config.enable_upload_file
            ),
            # Free space of the watched volumes and whether the guard has
            # paused downloads because it ran low (see module.disk_guard).
            "disk": disk_guard.status(),
        }
    )


@_flask_app.route("/set_download_state", methods=["POST"])
@login_required
def web_set_download_state():
    """Set download state"""
    state = request.args.get("state")

    # The value used to be echoed back verbatim as a text/html response, which
    # made this endpoint a reflected XSS. Accept only the two known commands.
    if state not in ("pause", "continue"):
        return "invalid state", 400, {"Content-Type": "text/plain; charset=utf-8"}

    if state == "continue" and get_download_state() is DownloadState.StopDownload:
        set_download_state(DownloadState.Downloading)
    elif state == "pause" and get_download_state() is DownloadState.Downloading:
        set_download_state(DownloadState.StopDownload)

    # Return the canonical next action, not the requested one. A double click or
    # a stale tab used to flip the button label to the opposite of the real state.
    next_action = (
        "pause" if get_download_state() is DownloadState.Downloading else "continue"
    )
    return next_action, 200, {"Content-Type": "text/plain; charset=utf-8"}


@_flask_app.route("/get_app_version")
@login_required
def get_app_version():
    """Get telegram_media_downloader version"""
    return utils.__version__


@_flask_app.route("/get_latest_version")
@login_required
def get_latest_version():
    """Latest GitHub release compared with this build.

    Served from the cached background check, so the endpoint never waits on
    the network; ``?force=1`` performs an on-demand check (used by the version
    chip in the header) and may take up to the request timeout.
    """
    if request.args.get("force") == "1":
        version_check.refresh(force=True)
    return jsonify(version_check.update_info())


def _progress_rows() -> list:
    """Snapshot the in-progress downloads as JSON-ready rows."""
    rows = []
    for chat_id, messages in get_download_result().items():
        for message_id, value in messages.items():
            total_size = value.get("total_size") or 0
            down_byte = value.get("down_byte") or 0
            file_name = value.get("file_name") or ""
            rows.append(
                {
                    "chat": chat_id,
                    "id": message_id,
                    "filename": os.path.basename(file_name),
                    "total_size": format_byte(total_size),
                    "download_progress": (
                        round(down_byte / total_size * 100, 1) if total_size else 0.0
                    ),
                    "download_speed": format_byte(value.get("download_speed") or 0)
                    + "/s",
                    "save_path": file_name.replace("\\", "/"),
                    "paused": is_task_paused(chat_id, message_id),
                }
            )
    rows.sort(key=lambda row: (str(row["chat"]), row["id"]))
    return rows


def _format_completed_row(value: dict) -> dict:
    file_name = value.get("file_name") or ""
    norm_path = file_name.replace("\\", "/")
    if _application is not None and _application.hide_file_name and norm_path:
        masked = f"****{os.path.splitext(norm_path)[-1]}"
        display_name = masked
        display_path = masked
    else:
        display_name = norm_path.rsplit("/", 1)[-1] if norm_path else ""
        display_path = norm_path
    return {
        "chat": value.get("chat"),
        "id": value.get("id"),
        "filename": display_name,
        "total_size": format_byte(value.get("total_size") or 0),
        "save_path": display_path,
        "completed_time": value.get("completed_time") or 0,
        "upload_success": bool(value.get("upload_success")),
        # "uploaded" (new) / "exists" (already on the remote) / None
        "upload_state": value.get("upload_state"),
        "uploading": upload_stat.is_uploading(value.get("chat"), value.get("id")),
    }


def _completed_rows() -> list:
    return [_format_completed_row(value) for value in get_completed_downloads()]


def _failed_rows() -> list:
    result = []
    for chat_id, messages in get_failed_downloads().items():
        for message_id, value in messages.items():
            result.append(
                {
                    "chat": chat_id,
                    "id": message_id,
                    "filename": value.get("file_name", ""),
                    "time": value.get("time", 0),
                    "reason": value.get("reason", ""),
                }
            )
    result.sort(key=lambda row: row.get("time", 0), reverse=True)
    return result


def _uploading_rows() -> list:
    """Shape the rclone progress rows for the Web UI "Uploading" tab."""
    rows = []
    for value in upload_stat.rows():
        file_name = str(value.get("file_name") or "").replace("\\", "/")
        rows.append(
            {
                "chat": value.get("chat"),
                "id": value.get("id"),
                "filename": file_name.rsplit("/", 1)[-1] if file_name else "",
                "save_path": file_name,
                "total_size": value.get("total") or "",
                "transferred": value.get("transferred") or "",
                "upload_progress": float(value.get("progress") or 0),
                "upload_speed": value.get("speed") or "",
                "eta": value.get("eta") or "",
            }
        )
    return rows


def _is_paginated_request() -> bool:
    return any(
        key in request.args
        for key in ("page", "page_size", "q", "upload_state", "paginated")
    )


def _parse_int_arg(name: str, default: int) -> int:
    raw = request.args.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _paginate_rows(rows: list, q: str, page: int, page_size: int) -> dict:
    """Filter and slice the in-memory progress list (the only non-SQL list)."""
    all_total = len(rows)
    query_text = (q or "").strip().lower()
    filtered = rows
    if query_text:
        filtered = [
            row
            for row in rows
            if query_text
            in " ".join(
                str(row.get(field) or "")
                for field in ("id", "chat", "filename", "save_path", "reason")
            ).lower()
        ]

    total = len(filtered)
    size = MAX_PAGE_SIZE if page_size <= 0 else min(max(1, page_size), MAX_PAGE_SIZE)
    pages = max(1, (total + size - 1) // size)
    cur_page = min(max(1, page), pages)
    start = (cur_page - 1) * size
    return {
        "rows": filtered[start : start + size],
        "total": total,
        "all_total": all_total,
        "page": cur_page,
        "page_size": size,
        "pages": pages,
    }


@_flask_app.route("/get_download_list")
@login_required
def get_download_list():
    """Return in-progress downloads (``already_down=false``) or finished ones.

    Without paging parameters the legacy full-list JSON is returned. With
    ``page``/``page_size``/``q``/``upload_state`` the finished list is filtered
    and paginated by SQLite, so a long history never travels in full.
    """
    already_down = request.args.get("already_down") == "true"
    paginated = _is_paginated_request()
    q = request.args.get("q", "")
    upload_state = request.args.get("upload_state", "all")
    page = _parse_int_arg("page", 1)
    page_size = _parse_int_arg("page_size", DEFAULT_PAGE_SIZE)

    if already_down:
        if not paginated:
            return jsonify(_completed_rows())
        result = query_completed_downloads(
            q=q, upload_state=upload_state, page=page, page_size=page_size
        )
        result["rows"] = [_format_completed_row(row) for row in result["rows"]]
        return jsonify(result)

    rows = _progress_rows()
    if not paginated:
        return jsonify(rows)
    return jsonify(_paginate_rows(rows, q=q, page=page, page_size=page_size))


@_flask_app.route("/get_failed_list")
@login_required
def get_failed_list():
    """List failed downloads so they can be retried from the Web UI."""
    if not _is_paginated_request():
        return jsonify(_failed_rows())

    q = request.args.get("q", "")
    page = _parse_int_arg("page", 1)
    page_size = _parse_int_arg("page_size", DEFAULT_PAGE_SIZE)
    return jsonify(query_failed_downloads(q=q, page=page, page_size=page_size))


@_flask_app.route("/get_uploading_list")
@login_required
def get_uploading_list():
    """In-flight cloud-drive uploads (rclone progress lines).

    The list lives in memory only: an upload lasts as long as the message task,
    so there is nothing worth persisting.
    """
    rows = _uploading_rows()
    if not _is_paginated_request():
        return jsonify(rows)

    q = request.args.get("q", "")
    page = _parse_int_arg("page", 1)
    page_size = _parse_int_arg("page_size", DEFAULT_PAGE_SIZE)
    return jsonify(_paginate_rows(rows, q=q, page=page, page_size=page_size))


def _task_args() -> Optional[Tuple[Union[int, str], int]]:
    """Parse ``chat_id``/``message_id`` from the query string.

    Numeric ids are converted back to int: the task dictionaries are keyed by
    the same types the downloader uses, and a string key made delete/pause a
    silent no-op.
    """
    chat_id = request.args.get("chat_id")
    message_id = request.args.get("message_id")
    if not chat_id or not message_id:
        return None
    try:
        return normalize_chat_id(chat_id), int(message_id)
    except ValueError:
        return None


@_flask_app.route("/pause_task", methods=["POST"])
@login_required
def pause_task():
    """Pause one message."""
    args = _task_args()
    if not args:
        return jsonify({"code": "0", "msg": "chat_id and message_id are required"})
    changed = set_task_paused(args[0], args[1], True)
    return jsonify({"code": "1", "changed": changed})


@_flask_app.route("/resume_task", methods=["POST"])
@login_required
def resume_task():
    """Resume one message."""
    args = _task_args()
    if not args:
        return jsonify({"code": "0", "msg": "chat_id and message_id are required"})
    changed = set_task_paused(args[0], args[1], False)
    return jsonify({"code": "1", "changed": changed})


@_flask_app.route("/delete_task", methods=["POST"])
@login_required
def delete_download_task():
    """Delete a task from every tab, aborting it when it is still downloading."""
    args = _task_args()
    if not args:
        return jsonify({"code": "0", "msg": "chat_id and message_id are required"})
    delete_task(args[0], args[1])
    if not is_task_cancelled(args[0], args[1]):
        _forget_failed_ids(args[0], args[1])
    return jsonify({"code": "1"})


@_flask_app.route("/retry_failed", methods=["POST"])
@login_required
def retry_failed():
    """Queue one failed message, one chat, or all failed messages again."""
    chat_id_arg = request.args.get("chat_id")
    message_id_arg = request.args.get("message_id")

    if not chat_id_arg and message_id_arg:
        return jsonify({"code": "0", "msg": "chat_id is required"})

    chat_id = normalize_chat_id(chat_id_arg) if chat_id_arg else None

    message_id = None
    if message_id_arg:
        try:
            message_id = int(message_id_arg)
        except ValueError:
            # None means "retry every failed message of the chat"; a single
            # typo must not trigger that bulk action.
            return jsonify({"code": "0", "msg": "invalid message_id"})

    if _retry_handler is None:
        return jsonify({"code": "0", "msg": "downloader is not ready"})

    reasons: list = []
    try:
        future = _retry_handler(chat_id, message_id)
        if future is not None:
            # Wait for the coroutine so the UI learns whether anything was
            # actually queued; before this it always answered {"code":"1"}.
            try:
                result = future.result(timeout=10)
            except futures.TimeoutError:
                # The coroutine keeps running, so the messages *will* be
                # queued: reporting a failure here made operators click retry
                # again and queue everything twice.
                return jsonify(
                    {
                        "code": "1",
                        "msg": "已提交，稍后刷新查看结果",
                        "pending": True,
                    }
                )
            except Exception as wait_err:  # noqa: BLE001 - becomes the API answer
                return jsonify({"code": "0", "msg": str(wait_err)})
            if isinstance(result, tuple):
                retried = int(result[0] or 0)
                reasons = [str(reason) for reason in (result[1] or []) if reason]
            else:
                retried = int(result or 0)
        else:
            retried = 0
    except Exception as e:  # pylint: disable = W0703
        return jsonify({"code": "0", "msg": str(e)})

    if not retried:
        return jsonify(
            {
                "code": "0",
                "msg": reasons[0] if reasons else "没有匹配的失败任务",
            }
        )
    return jsonify({"code": "1", "retried": retried})


@_flask_app.route("/retry_upload", methods=["POST"])
@login_required
def retry_upload():
    """Upload one locally kept file again (completed tab, "仅本地" rows)."""
    args = _task_args()
    if not args:
        return jsonify({"code": "0", "msg": "chat_id and message_id are required"})

    if _upload_handler is None:
        return jsonify({"code": "0", "msg": "downloader is not ready"})

    try:
        future = _upload_handler(args[0], args[1])
        if future is None:
            return jsonify({"code": "0", "msg": "上传任务未启动"})
        try:
            result = future.result(timeout=10)
        except futures.TimeoutError:
            # The upload itself keeps running; polling refreshes the row.
            return jsonify(
                {
                    "code": "1",
                    "pending": True,
                    "msg": "已提交重新上传，稍后刷新查看结果",
                }
            )
        except Exception as wait_err:  # noqa: BLE001 - becomes the API answer
            return jsonify({"code": "0", "msg": str(wait_err)})
    except Exception as e:  # pylint: disable = W0703
        return jsonify({"code": "0", "msg": str(e)})

    ok, payload = result if isinstance(result, tuple) else (bool(result), "")
    if not ok:
        return jsonify({"code": "0", "msg": payload or "上传失败"})
    return jsonify({"code": "1", "upload_state": payload})


def _apply_forget(chat_config, message_id: Optional[int]) -> None:
    """Drop the ids from the retry bookkeeping (must run on the loop)."""
    ids = [message_id] if message_id is not None else list(chat_config.ids_to_retry)
    for one_id in ids:
        if one_id in chat_config.ids_to_retry:
            chat_config.ids_to_retry.remove(one_id)
        chat_config.ids_to_retry_dict.pop(one_id, None)
        chat_config.node.download_status.pop(one_id, None)


def _forget_failed_ids(chat_id, message_id: Optional[int]) -> None:
    """Also drop the id from ``ids_to_retry`` so a restart cannot re-queue it."""
    # These structures belong to the download loop: resizing
    # ``download_status`` from the Flask thread can raise
    # "dictionary changed size during iteration". Schedule the change onto
    # the loop; call it directly when the loop is not running (tests,
    # shutdown).
    if _application is None:
        return
    chat_config = _application.chat_download_config.get(chat_id)
    if chat_config is None:
        return
    loop = getattr(_application, "loop", None)
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(_apply_forget, chat_config, message_id)
    else:
        _apply_forget(chat_config, message_id)


@_flask_app.route("/clear_failed", methods=["POST"])
@login_required
def clear_failed():
    """Forget recorded failures without retrying them."""
    chat_id_arg = request.args.get("chat_id")
    message_id_arg = request.args.get("message_id")

    if chat_id_arg is None:
        cleared = clear_failed_downloads()
        if _application is not None:
            for chat_id in list(_application.chat_download_config):
                _forget_failed_ids(chat_id, None)
        return jsonify({"code": "1", "cleared": cleared})

    chat_id = normalize_chat_id(chat_id_arg)

    if message_id_arg:
        try:
            one_id = int(message_id_arg)
        except ValueError:
            return jsonify({"code": "0", "msg": "invalid message_id"})
        mark_download_retried(chat_id, one_id)
        _forget_failed_ids(chat_id, one_id)
        return jsonify({"code": "1", "cleared": 1})

    cleared = clear_failed_downloads(chat_id)
    _forget_failed_ids(chat_id, None)
    return jsonify({"code": "1", "cleared": cleared})


@_flask_app.route("/clear_completed", methods=["POST"])
@login_required
def clear_completed():
    """Clear recorded completed downloads without touching local files."""
    chat_id_arg = request.args.get("chat_id")
    if not chat_id_arg:
        cleared = clear_completed_downloads()
        return jsonify({"code": "1", "cleared": cleared})

    cleared = clear_completed_downloads(normalize_chat_id(chat_id_arg))
    return jsonify({"code": "1", "cleared": cleared})


# --------------------------------------------------------------------- config


def _config_path() -> Optional[str]:
    if _application is None:
        return None
    return os.path.join(os.path.abspath("."), _application.config_file)


def _mask_config_text(text: str) -> str:
    """Replace secret values so they never leave the server."""
    lines = []
    for line in text.splitlines():
        match = _SECRET_LINE.match(line)
        if match and match.group(4).strip().strip("\"'"):
            line = f"{match.group(1)}{match.group(2)}{match.group(3)}{CONFIG_MASK}"
        lines.append(line)
    suffix = "\n" if text.endswith("\n") else ""
    return "\n".join(lines) + suffix


def _restore_masked_config_text(new_text: str, current_text: str) -> str:
    """Put the real secret back when the submitted field still holds the mask."""
    current = {}
    for line in current_text.splitlines():
        match = _SECRET_LINE.match(line)
        if match:
            current[match.group(2)] = match.group(4).strip()

    lines = []
    for line in new_text.splitlines():
        match = _SECRET_LINE.match(line)
        if (
            match
            and match.group(4).strip().strip("\"'") == CONFIG_MASK
            and current.get(match.group(2))
        ):
            line = f"{match.group(1)}{match.group(2)}{match.group(3)}{current[match.group(2)]}"
        lines.append(line)
    suffix = "\n" if new_text.endswith("\n") else ""
    return "\n".join(lines) + suffix


@_flask_app.route("/get_config")
@login_required
def get_config():
    """Return config.yaml as text with the secrets masked."""
    path = _config_path()
    if not path or not os.path.exists(path):
        return jsonify({"code": "0", "msg": "config.yaml not found"})

    with open(path, encoding="utf-8") as config_file:
        text = config_file.read()

    return jsonify(
        {
            "code": "1",
            "path": path,
            "text": _mask_config_text(text),
            "masked_keys": list(SECRET_CONFIG_KEYS),
        }
    )


def _load_current_config(path: str):
    """Read and parse config.yaml, returning ``(config, error_response)``."""
    with open(path, encoding="utf-8") as config_file:
        text = config_file.read()

    try:
        config = _yaml.load(text)
    except Exception as parse_err:  # pylint: disable = W0703
        return None, jsonify({"code": "0", "msg": f"YAML 解析失败：{parse_err}"})

    if not isinstance(config, dict):
        return None, jsonify({"code": "0", "msg": "配置必须是 YAML 映射（key: value）"})

    return config, None


@_flask_app.route("/get_config_form")
@login_required
def get_config_form():
    """Return the form schema, the current values (masked) and the raw YAML."""
    path = _config_path()
    if not path or not os.path.exists(path):
        return jsonify({"code": "0", "msg": "config.yaml not found"})

    config, error = _load_current_config(path)
    if error is not None:
        return error

    with open(path, encoding="utf-8") as config_file:
        text = config_file.read()

    payload = config_form.schema()
    payload.update(
        {
            "code": "1",
            "path": path,
            "values": config_form.read_values(config),
            "text": _mask_config_text(text),
        }
    )
    return jsonify(payload)


@_flask_app.route("/set_config_form", methods=["POST"])
@login_required
def set_config_form():
    """Validate the submitted form, write config.yaml and hot-reload it."""
    path = _config_path()
    if not path or not os.path.exists(path):
        return jsonify({"code": "0", "msg": "config.yaml not found"})

    config, error = _load_current_config(path)
    if error is not None:
        return error

    values, errors = config_form.parse_form(request.form, config)
    if errors:
        return jsonify(
            {"code": "0", "errors": errors, "msg": "请检查标红的设置项"}
        )

    config_form.apply_values(config, values)
    errors = config_form.validate(config)
    if errors:
        return jsonify(
            {"code": "0", "errors": errors, "msg": "请检查标红的设置项"}
        )

    stream = io.StringIO()
    try:
        _yaml.dump(config, stream)
    except Exception as dump_err:  # pylint: disable = W0703
        return jsonify({"code": "0", "msg": f"生成 YAML 失败：{dump_err}"})

    new_text = stream.getvalue()
    try:
        write_text_atomic(path, new_text)
    except OSError as write_err:
        return jsonify({"code": "0", "msg": f"写入失败：{write_err}"})

    applied, restart_required = [], []
    if _application is not None:
        applied, restart_required = _application.reload_hot_config(config)
        _refresh_login_config(_application)

    app_log.info("config.yaml updated from the Web UI form: {}", applied)
    return jsonify(
        {
            "code": "1",
            "applied": applied,
            "restart_required": restart_required,
            "values": config_form.read_values(config),
            "text": _mask_config_text(new_text),
        }
    )


@_flask_app.route("/set_config", methods=["POST"])
@login_required
def set_config():
    """Validate and save config.yaml, then apply the hot-reloadable keys."""
    path = _config_path()
    if not path or not os.path.exists(path):
        return jsonify({"code": "0", "msg": "config.yaml not found"})

    new_text = request.form.get("text", "")
    if not new_text.strip():
        return jsonify({"code": "0", "msg": "配置内容不能为空"})

    with open(path, encoding="utf-8") as config_file:
        current_text = config_file.read()

    restored_text = _restore_masked_config_text(new_text, current_text)

    try:
        parsed = _yaml.load(restored_text)
    except Exception as parse_err:  # pylint: disable = W0703
        return jsonify({"code": "0", "msg": f"YAML 解析失败：{parse_err}"})

    if not isinstance(parsed, dict):
        return jsonify({"code": "0", "msg": "配置必须是 YAML 映射（key: value）"})

    missing = [key for key in ("api_id", "api_hash", "media_types") if key not in parsed]
    if missing:
        return jsonify({"code": "0", "msg": "缺少必填项：" + ", ".join(missing)})

    try:
        write_text_atomic(path, restored_text)
    except OSError as write_err:
        return jsonify({"code": "0", "msg": f"写入失败：{write_err}"})

    applied, restart_required = [], []
    if _application is not None:
        applied, restart_required = _application.reload_hot_config(parsed)
        _refresh_login_config(_application)

    app_log.info("config.yaml updated from the Web UI: {}", applied)
    return jsonify(
        {
            "code": "1",
            "applied": applied,
            "restart_required": restart_required,
        }
    )
