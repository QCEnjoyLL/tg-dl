"""web ui for media download"""

import hmac
import io
import logging
import os
import re
import secrets
import tempfile
import threading
from typing import Optional, Tuple, Union

from flask import Flask, jsonify, redirect, render_template, request
from flask_login import LoginManager, UserMixin, login_required, login_user, logout_user
from ruamel import yaml
from werkzeug.security import check_password_hash

import utils
from module import config_form
from module.app import Application
from module.download_stat import (
    DownloadState,
    clear_failed_downloads,
    delete_task,
    get_completed_downloads,
    get_download_result,
    get_download_state,
    get_failed_downloads,
    get_total_download_speed,
    is_task_paused,
    mark_download_retried,
    set_download_state,
    normalize_chat_id,
    set_task_paused,
)
from utils.format import format_byte

log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)
app_log = logging.getLogger(__name__)

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
_application: Optional[Application] = None


def set_retry_handler(handler):
    """Register the callback used by /retry_failed to re-queue a message."""
    global _retry_handler
    _retry_handler = handler


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

    get_flask_app().run(host, app.web_port, debug=app.debug_web, use_reloader=False)


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
            "127.0.0.1 instead of %s. Set web_login_secret to expose it.",
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
        username = "root"
        # TLS terminates in front of the app; the previous AES-in-JS layer used
        # a key shipped with the page and added no protection over plain HTTP.
        password = request.form.get("password", "")

        if not password:
            return jsonify({"code": "0"})

        secret = web_login_users.get(username)
        if secret and _verify_password(str(secret), password):
            user = User()
            login_user(user)
            return jsonify({"code": "1"})

        return jsonify({"code": "0"})

    return render_template("login.html", version=utils.__version__)


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
    """Get download speed"""
    return jsonify(
        {
            "download_speed": format_byte(get_total_download_speed()) + "/s",
            "upload_speed": "0.00 B/s",
            "state": (
                "running"
                if get_download_state() is DownloadState.Downloading
                else "paused"
            ),
            # Keeps the "已完成" tab badge fresh without polling the full list.
            "completed": len(get_completed_downloads()),
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


def _completed_rows() -> list:
    rows = []
    for value in get_completed_downloads():
        file_name = value.get("file_name") or ""
        rows.append(
            {
                "chat": value.get("chat"),
                "id": value.get("id"),
                "filename": os.path.basename(file_name),
                "total_size": format_byte(value.get("total_size") or 0),
                "save_path": file_name.replace("\\", "/"),
                "completed_time": value.get("completed_time") or 0,
                "upload_success": bool(value.get("upload_success")),
                # "uploaded" (new) / "exists" (already on the remote) / None
                "upload_state": value.get("upload_state"),
            }
        )
    return rows


@_flask_app.route("/get_download_list")
@login_required
def get_download_list():
    """Return in-progress downloads (``already_down=false``) or finished ones."""
    already_down = request.args.get("already_down") == "true"
    if already_down:
        return jsonify(_completed_rows())
    return jsonify(_progress_rows())


@_flask_app.route("/get_failed_list")
@login_required
def get_failed_list():
    """List failed downloads so they can be retried from the Web UI."""
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
    return jsonify(result)


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
    return jsonify({"code": "1"})


@_flask_app.route("/retry_failed", methods=["POST"])
@login_required
def retry_failed():
    """Queue one failed message again, or the whole chat when id is omitted."""
    chat_id_arg = request.args.get("chat_id")
    message_id_arg = request.args.get("message_id")

    if chat_id_arg is None:
        return jsonify({"code": "0", "msg": "chat_id is required"})

    chat_id = normalize_chat_id(chat_id_arg)

    message_id = None
    if message_id_arg:
        try:
            message_id = int(message_id_arg)
        except ValueError:
            message_id = None

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


def _forget_failed_ids(chat_id, message_id: Optional[int]) -> None:
    """Also drop the id from ``ids_to_retry`` so a restart cannot re-queue it."""
    if _application is None:
        return
    chat_config = _application.chat_download_config.get(chat_id)
    if chat_config is None:
        return
    ids = [message_id] if message_id is not None else list(chat_config.ids_to_retry)
    for one_id in ids:
        if one_id in chat_config.ids_to_retry:
            chat_config.ids_to_retry.remove(one_id)
        chat_config.ids_to_retry_dict.pop(one_id, None)
        chat_config.node.download_status.pop(one_id, None)


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

    try:
        chat_id = int(chat_id_arg)
    except ValueError:
        chat_id = chat_id_arg

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


def _write_config_atomic(path: str, text: str) -> None:
    """Write the file through a temp file so a crash cannot truncate it.

    Docker setups bind-mount ``config.yaml`` as a single file, and ``os.replace``
    onto a mount point fails with EBUSY. Fall back to an in-place write there
    instead of leaving the config unsaveable.
    """
    directory = os.path.dirname(path) or "."
    handle, temp_path = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".yaml")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as config_file:
            config_file.write(text)
            config_file.flush()
            os.fsync(config_file.fileno())
        os.replace(temp_path, path)
    except OSError:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        with open(path, "w", encoding="utf-8") as config_file:
            config_file.write(text)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


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
        _write_config_atomic(path, new_text)
    except OSError as write_err:
        return jsonify({"code": "0", "msg": f"写入失败：{write_err}"})

    applied, restart_required = [], []
    if _application is not None:
        applied, restart_required = _application.reload_hot_config(config)
        _refresh_login_config(_application)

    app_log.info("config.yaml updated from the Web UI form: %s", applied)
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
        _write_config_atomic(path, restored_text)
    except OSError as write_err:
        return jsonify({"code": "0", "msg": f"写入失败：{write_err}"})

    applied, restart_required = [], []
    if _application is not None:
        applied, restart_required = _application.reload_hot_config(parsed)
        _refresh_login_config(_application)

    app_log.info("config.yaml updated from the Web UI: %s", applied)
    return jsonify(
        {
            "code": "1",
            "applied": applied,
            "restart_required": restart_required,
        }
    )
