#!/usr/bin/env python3
"""Render the Flask templates as static preview pages with mocked API data.

The preview only exists so the UI can be checked in a browser without a
Telegram session. It is excluded from the Docker image via .dockerignore.

    python preview/build.py
    python -m http.server 8123 --directory .
    open http://127.0.0.1:8123/preview/index.html
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "module" / "templates"
OUT = Path(__file__).resolve().parent

# The config schema is pure stdlib, so the preview can reuse the real one
# instead of duplicating every field.
sys.path.insert(0, str(ROOT))
from module import config_form  # noqa: E402  pylint: disable = wrong-import-position

URL_FOR = re.compile(
    r"\{\{\s*url_for\('static',\s*filename='([^']+)'(?:,\s*v=version)?\)\s*\}\}"
)
VERSION_RE = re.compile(r'__version__\s*=\s*"([^"]+)"')

NOW = 1763000000


def project_version() -> str:
    """Read __version__ without importing the package (needs no deps)."""
    text = (ROOT / "utils" / "__init__.py").read_text(encoding="utf-8")
    match = VERSION_RE.search(text)
    return match.group(1) if match else "0.0.0"


VERSION = project_version()

DOWNLOADING = [
    {
        "chat": "@example_channel",
        "id": "392",
        "filename": "392 - 示例视频_01.mov",
        "total_size": "2.09GB",
        "download_progress": "82.6",
        "download_speed": "12.48MB/s",
        "paused": False,
    },
    {
        "chat": "@example_channel",
        "id": "393",
        "filename": "393 - 示例视频_02.mov",
        "total_size": "2.3GB",
        "download_progress": "77.0",
        "download_speed": "10.99MB/s",
        "paused": True,
    },
    {
        "chat": "@example_channel",
        "id": "405",
        "filename": "405 - 示例视频_03.mov",
        "total_size": "2.01GB",
        "download_progress": "48.3",
        "download_speed": "8.34MB/s",
        "paused": False,
    },
    {
        "chat": "@example_channel",
        "id": "790",
        "filename": "790 - 示例视频_04.mp4",
        "total_size": "89.1MB",
        "download_progress": "52.7",
        "download_speed": "5.99MB/s",
        "paused": False,
    },
]

DOWNLOADED = [
    {
        "chat": "@example_channel",
        "id": "388",
        "filename": "388 - 示例视频_05.mov",
        "total_size": "2.12GB",
        "completed_time": NOW - 120,
        "upload_state": "uploaded",
        "upload_success": True,
        "save_path": "/app/downloads/示例频道/2026_11/388 - 示例视频_05.mov",
    },
    {
        "chat": "@example_channel",
        "id": "390",
        "filename": "390 - 示例视频_06.mov",
        "total_size": "1.95GB",
        "completed_time": NOW - 900,
        "upload_state": "exists",
        "upload_success": True,
        "save_path": "/app/downloads/示例频道/2026_11/390 - 示例视频_06.mov",
    },
    {
        "chat": "@example_channel",
        "id": "772",
        "filename": "772 - 示例视频_07.mp4",
        "total_size": "928.01MB",
        "completed_time": NOW - 3600,
        "upload_state": None,
        "upload_success": False,
        "save_path": "/app/downloads/示例频道/2026_11/772 - 示例视频_07.mp4",
    },
]

# Extra completed rows so the preview (and the docs screenshots) show the
# 25-per-page pagination instead of a single short table.
_DEMO_TITLES = [
    "示例视频_18",
    "示例视频_19",
    "示例视频_10",
    "示例视频_11",
    "示例视频_12",
    "示例视频_13",
    "示例视频_14",
    "示例视频_15",
    "示例视频_16",
    "示例视频_17",
]
for _index in range(27):
    _num = 700 - _index * 7
    _title = _DEMO_TITLES[_index % len(_DEMO_TITLES)]
    DOWNLOADED.append(
        {
            "chat": "@example_channel",
            "id": str(_num),
            "filename": f"{_num} - {_title}.mp4",
            "total_size": f"{(_index % 9) + 1}.{_index % 10}GB",
            "completed_time": NOW - 7200 - _index * 1800,
            "upload_state": ["uploaded", "exists", None][_index % 3],
            "upload_success": _index % 3 != 2,
            "save_path": f"/app/downloads/示例频道/2026_11/{_num} - {_title}.mp4",
        }
    )

FAILED = [
    {
        "chat": "@example_channel",
        "id": "132",
        "filename": "132 - 示例文档_01.zip",
        "time": NOW - 900,
        "reason": "file reference expired",
    },
    {
        "chat": "@example_channel",
        "id": "785",
        "filename": "785 - 示例视频_08.mov",
        "time": NOW - 3600,
        "reason": "only received 0 of 10485760 bytes (Timeout: Telegram says: [500 TIMEOUT] - A timeout occurred while fetching data from the worker); download failed due to network timeout",
    },
    {
        "chat": "@example_channel",
        "id": "801",
        "filename": "801 - 会议纪要_录音转写.mp4",
        "time": NOW - 7200,
        "reason": "FloodWait: 420 seconds",
    },
]

MOCK_CONFIG = """api_hash: ********
api_id: 12345678

chat:
- chat_id: @example_channel
  last_read_message_id: 0

media_types:
- video
- document

max_download_task: 2
max_concurrent_transmissions: 4

upload_drive:
  enable_upload_file: true
  upload_adapter: rclone
  remote_dir: "115:Telegram"
  rclone_path: /app/rclone/rclone
  after_upload_file_delete: true

web_host: 0.0.0.0
web_port: 5000
language: ZH
"""

FORM_VALUES = {
    "api_id": "12345678",
    "api_hash": config_form.MASK,
    "bot_token": "",
    "chat": "@example_channel\n@another_channel",
    "media_types": ["video", "document"],
    "save_path": "/app/downloads",
    "language": "ZH",
    "allowed_user_ids": "me",
    "max_download_task": "2",
    "max_concurrent_transmissions": "4",
    "start_timeout": "60",
    "forward_limit": "33",
    "file_formats.audio": "all",
    "file_formats.document": "all",
    "file_formats.video": "all",
    "file_path_prefix": ["chat_title", "media_datetime"],
    "file_name_prefix": ["message_id", "file_name"],
    "file_name_prefix_split": " - ",
    "date_format": "%Y_%m",
    "hide_file_name": False,
    "enable_download_txt": False,
    "after_upload_telegram_delete": True,
    "drop_no_audio_video": False,
    "upload_drive.enable_upload_file": True,
    "upload_drive.upload_adapter": "rclone",
    "upload_drive.remote_dir": "115:Telegram",
    "upload_drive.rclone_path": "/app/rclone/rclone",
    "upload_drive.before_upload_file_zip": False,
    "upload_drive.after_upload_file_delete": True,
    "proxy.scheme": "",
    "proxy.hostname": "",
    "proxy.port": "",
    "proxy.username": "",
    "proxy.password": "",
    "web_host": "0.0.0.0",
    "web_port": "5000",
    "web_login_secret": config_form.MASK,
    "log_level": "INFO",
    "filter_advertisement_list": "",
    "replace_advertisement_list": "",
}

FORM_PAYLOAD = {
    "code": "1",
    "path": "/app/config.yaml",
    "fields": config_form.FIELDS,
    "groups": config_form.GROUPS,
    "values": FORM_VALUES,
    "text": MOCK_CONFIG,
}

MOCK_JS = """
<script>
(function () {
  var paused = false;
  var data = %s;
  var configText = %s;
  var formPayload = %s;
  var pageVersion = %s;
  function respond(body, type) {
    return new Response(body, { status: 200, headers: { 'content-type': type || 'application/json' } });
  }
  window.fetch = function (url, options) {
    var u = String(url);
    if (u.indexOf('/get_app_version') !== -1) {
      return Promise.resolve(respond(pageVersion, 'text/plain'));
    }
    if (u.indexOf('/get_download_status') !== -1) {
      return Promise.resolve(respond(JSON.stringify({ download_speed: '4.28 MB/s', upload_speed: '0.00 B/s', state: paused ? 'paused' : 'running', completed: data.downloaded.length })));
    }
    if (u.indexOf('already_down=false') !== -1) {
      return Promise.resolve(respond(JSON.stringify(data.downloading)));
    }
    if (u.indexOf('already_down=true') !== -1) {
      return Promise.resolve(respond(JSON.stringify(data.downloaded)));
    }
    if (u.indexOf('/get_failed_list') !== -1) {
      return Promise.resolve(respond(JSON.stringify(data.failed)));
    }
    if (u.indexOf('/set_download_state') !== -1) {
      paused = !paused;
      return Promise.resolve(respond(paused ? 'continue' : 'pause', 'text/plain'));
    }
    if (u.indexOf('/get_config_form') !== -1) {
      return Promise.resolve(respond(JSON.stringify(formPayload)));
    }
    if (u.indexOf('/get_config') !== -1) {
      return Promise.resolve(respond(JSON.stringify({ code: '1', path: '/app/config.yaml', text: configText })));
    }
    if (u.indexOf('/set_config') !== -1) {
      return Promise.resolve(respond(JSON.stringify({ code: '1', applied: ['media_types'], restart_required: ['chat'], values: formPayload.values, text: configText })));
    }
    function params(url) {
      var out = {};
      var query = url.split('?')[1] || '';
      query.split('&').forEach(function (part) {
        if (!part) return;
        var kv = part.split('=');
        out[decodeURIComponent(kv[0])] = decodeURIComponent(kv[1] || '');
      });
      return out;
    }
    function drop(list, p) {
      for (var i = list.length - 1; i >= 0; i--) {
        if (String(list[i].id) === String(p.message_id) &&
            (p.chat_id === undefined || String(list[i].chat) === String(p.chat_id))) {
          list.splice(i, 1);
          return true;
        }
      }
      return false;
    }
    var p = params(u);
    if (u.indexOf('/pause_task') !== -1 || u.indexOf('/resume_task') !== -1) {
      var on = u.indexOf('/pause_task') !== -1;
      data.downloading.forEach(function (row) {
        if (String(row.id) === String(p.message_id)) row.paused = on;
      });
      return Promise.resolve(respond(JSON.stringify({ code: '1', changed: true })));
    }
    if (u.indexOf('/delete_task') !== -1) {
      drop(data.downloading, p);
      drop(data.downloaded, p);
      drop(data.failed, p);
      return Promise.resolve(respond(JSON.stringify({ code: '1' })));
    }
    if (u.indexOf('/retry_failed') !== -1) {
      var retried = 0;
      for (var i = data.failed.length - 1; i >= 0; i--) {
        var matchChat = !p.chat_id || String(data.failed[i].chat) === String(p.chat_id);
        var matchMsg = !p.message_id || String(data.failed[i].id) === String(p.message_id);
        if (matchChat && matchMsg) {
          var failedRow = data.failed.splice(i, 1)[0];
          data.downloading.push({
            chat: failedRow.chat,
            id: failedRow.id,
            filename: failedRow.filename,
            total_size: '0B',
            download_progress: 0,
            download_speed: '0 B/s',
            paused: false
          });
          retried += 1;
        }
      }
      return Promise.resolve(respond(JSON.stringify({ code: '1', retried: retried })));
    }
    if (u.indexOf('/clear_failed') !== -1) {
      data.failed.length = 0;
      return Promise.resolve(respond(JSON.stringify({ code: '1', cleared: 3 })));
    }
    return Promise.resolve(respond('[]'));
  };
})();
</script>
""" % (
    json.dumps(
        {"downloading": DOWNLOADING, "downloaded": DOWNLOADED, "failed": FAILED},
        ensure_ascii=False,
    ),
    json.dumps(MOCK_CONFIG, ensure_ascii=False),
    json.dumps(FORM_PAYLOAD, ensure_ascii=False, default=str),
    json.dumps(VERSION),
)


def render(name: str, state: str = "pause") -> str:
    html = (TEMPLATES / name).read_text(encoding="utf-8")
    html = URL_FOR.sub(
        lambda m: f"../module/static/{m.group(1)}?v={VERSION}", html
    )
    html = html.replace("{{ version }}", VERSION)
    html = html.replace("{{ download_state }}", state)
    # The preview has no session, so pretend the login is enabled to show the
    # logout button; strip the Jinja conditional around it.
    html = html.replace("{% if login_enabled %}", "")
    html = html.replace("{% endif %}", "")

    # Inject the mocked fetch layer before the application script runs. The tag
    # carries a ?v=<version> cache-buster, so match it loosely.
    marker = re.compile(
        r'(<script src="\.\./module/static/js/app\.js[^"]*"></script>)'
    )
    html = marker.sub(lambda m: MOCK_JS + m.group(1), html, count=1)
    return html


def main() -> None:
    (OUT / "index.html").write_text(render("index.html"), encoding="utf-8")
    (OUT / "login.html").write_text(render("login.html"), encoding="utf-8")
    print("preview written to", OUT, "(version", VERSION + ")")


if __name__ == "__main__":
    main()
