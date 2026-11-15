"""web ui for media download"""

import hmac
import logging
import os
import secrets
import threading

from flask import Flask, jsonify, render_template, request
from flask_login import LoginManager, UserMixin, login_required, login_user
from werkzeug.security import check_password_hash

import utils
from module.app import Application
from module.download_stat import (
    DownloadState,
    get_download_result,
    get_download_state,
    get_failed_downloads,
    get_total_download_speed,
    set_download_state,
)
from utils.format import format_byte

log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)
app_log = logging.getLogger(__name__)

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

_login_manager = LoginManager()
_login_manager.login_view = "login"
_login_manager.init_app(_flask_app)
web_login_users: dict = {}
_retry_handler = None


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
    if app.web_login_secret:
        web_login_users = {"root": app.web_login_secret}
        _flask_app.config["LOGIN_DISABLED"] = False
    else:
        web_login_users = {}
        _flask_app.config["LOGIN_DISABLED"] = True

    host = _effective_web_host(app)
    if app.debug_web:
        # Non-daemon threads keep the process alive after main() returns, which
        # turns `docker stop` into a 2 minute wait followed by SIGKILL.
        threading.Thread(target=run_web_server, args=(app, host), daemon=True).start()
    else:
        threading.Thread(
            target=get_flask_app().run, daemon=True, args=(host, app.web_port)
        ).start()


@_flask_app.after_request
def _set_security_headers(response):
    """Cheap, safe defaults; the UI never needs to be framed or MIME-sniffed."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
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

    return render_template("login.html")


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
    )


@_flask_app.route("/get_download_status")
@login_required
def get_download_speed():
    """Get download speed"""
    return jsonify(
        {
            "download_speed": format_byte(get_total_download_speed()) + "/s",
            "upload_speed": "0.00 B/s",
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


@_flask_app.route("/get_download_list")
@login_required
def get_download_list():
    """get download list"""
    if request.args.get("already_down") is None:
        return jsonify([])

    already_down = request.args.get("already_down") == "true"

    download_result = get_download_result()
    result = []
    for chat_id, messages in download_result.items():
        for idx, value in messages.items():
            total_size = value.get("total_size") or 0
            down_byte = value.get("down_byte") or 0
            is_already_down = down_byte == total_size

            if already_down and not is_already_down:
                continue

            # Progress callbacks can report total_size == 0 for media whose size
            # Telegram did not expose; never divide by it.
            download_progress = (
                round(down_byte / total_size * 100, 1) if total_size else 0.0
            )
            file_name = value.get("file_name") or ""
            result.append(
                {
                    "chat": chat_id,
                    "id": idx,
                    "filename": os.path.basename(file_name),
                    "total_size": format_byte(total_size),
                    "download_progress": download_progress,
                    "download_speed": format_byte(value.get("download_speed") or 0)
                    + "/s",
                    "save_path": file_name.replace("\\", "/"),
                }
            )

    # Return real JSON. The Web UI only calls response.json() for a JSON
    # content type; returning a hand-built string made every list render empty
    # in a real deployment (the static preview mocked application/json).
    return jsonify(result)


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
    return jsonify(result)


@_flask_app.route("/retry_failed", methods=["POST"])
@login_required
def retry_failed():
    """Queue one failed message again, or the whole chat when id is omitted."""
    chat_id_arg = request.args.get("chat_id")
    message_id_arg = request.args.get("message_id")

    if chat_id_arg is None:
        return jsonify({"code": "0", "msg": "chat_id is required"})

    try:
        chat_id = int(chat_id_arg)
    except ValueError:
        chat_id = chat_id_arg

    message_id = None
    if message_id_arg:
        try:
            message_id = int(message_id_arg)
        except ValueError:
            message_id = None

    if _retry_handler is None:
        return jsonify({"code": "0", "msg": "downloader is not ready"})

    try:
        _retry_handler(chat_id, message_id)
    except Exception as e:  # pylint: disable = W0703
        return jsonify({"code": "0", "msg": str(e)})

    return jsonify({"code": "1"})


@_flask_app.route("/clear_failed", methods=["POST"])
@login_required
def clear_failed():
    """Forget recorded failures without retrying them."""
    # pylint: disable = C0415
    from module.download_stat import clear_failed_downloads, mark_download_retried

    chat_id_arg = request.args.get("chat_id")
    message_id_arg = request.args.get("message_id")

    if chat_id_arg is None:
        return jsonify({"code": "1", "cleared": clear_failed_downloads()})

    try:
        chat_id = int(chat_id_arg)
    except ValueError:
        chat_id = chat_id_arg

    if message_id_arg:
        try:
            mark_download_retried(chat_id, int(message_id_arg))
            return jsonify({"code": "1", "cleared": 1})
        except ValueError:
            return jsonify({"code": "0", "msg": "invalid message_id"})

    return jsonify({"code": "1", "cleared": clear_failed_downloads(chat_id)})

