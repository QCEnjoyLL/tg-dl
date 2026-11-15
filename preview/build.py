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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "module" / "templates"
OUT = Path(__file__).resolve().parent

URL_FOR = re.compile(r"\{\{\s*url_for\('static',\s*filename='([^']+)'\)\s*\}\}")

NOW = 1763000000

DOWNLOADING = [
    {"chat": "-1001234567890", "id": "392", "filename": "392 - 城市夜景延时_4K.mov",
     "total_size": "2.09GB", "download_progress": "82.6", "download_speed": "12.48MB/s"},
    {"chat": "-1001234567890", "id": "393", "filename": "393 - 纪录片_第4集_未剪辑.mov",
     "total_size": "2.3GB", "download_progress": "77.0", "download_speed": "10.99MB/s"},
    {"chat": "-1001234567890", "id": "405",
     "filename": "405 - copy_G4DAB80_F7A_4E81_BF62_8F23D25DGAB_master_v2.mov",
     "total_size": "2.01GB", "download_progress": "48.3", "download_speed": "8.34MB/s"},
    {"chat": "-1001234567890", "id": "790", "filename": "790 - 航拍素材_海边.mp4",
     "total_size": "89.1MB", "download_progress": "52.7", "download_speed": "5.99MB/s"},
    {"chat": "-1001234567890", "id": "804", "filename": "804 - 产品演示_屏幕录制.mp4",
     "total_size": "42.69MB", "download_progress": "82.0", "download_speed": "6.07MB/s"},
    {"chat": "-1001234567890", "id": "803", "filename": "803 - 课程录像_第12讲_完整版.mp4",
     "total_size": "253.76MB", "download_progress": "13.8", "download_speed": "5.85MB/s"},
    {"chat": "-1001234567890", "id": "401", "filename": "401 - 采访原片_A机位.mov",
     "total_size": "1.49GB", "download_progress": "3.0", "download_speed": "4.16MB/s"},
    {"chat": "-1001234567890", "id": "427", "filename": "427 - 旅行vlog_巴厘岛_第1天.mov",
     "total_size": "1.8GB", "download_progress": "7.6", "download_speed": "3.29MB/s"},
]

DOWNLOADED = [
    {"chat": "-1001234567890", "id": "388", "filename": "388 - 城市风光_合集_01.mov",
     "total_size": "2.12GB", "save_path": "/app/downloads/示例频道/2026_11/388 - 城市风光_合集_01.mov"},
    {"chat": "-1001234567890", "id": "390", "filename": "390 - 延时摄影_日落.mov",
     "total_size": "1.95GB", "save_path": "/app/downloads/示例频道/2026_11/390 - 延时摄影_日落.mov"},
    {"chat": "-1001234567890", "id": "772", "filename": "772 - 直播回放_产品发布会_全程_with_chat.mp4",
     "total_size": "928.01MB", "save_path": "/app/downloads/示例频道/2026_11/772 - 直播回放_产品发布会.mp4"},
    {"chat": "-1001234567890", "id": "777", "filename": "777 - 教学视频_第07课.mov",
     "total_size": "2.58GB", "save_path": "/app/downloads/示例频道/2026_11/777 - 教学视频_第07课.mov"},
]

FAILED = [
    {"chat": "-1001234567890", "id": "132", "filename": "132 - 素材包_2026_第3批.zip",
     "time": NOW - 900, "reason": "file reference expired"},
    {"chat": "-1001234567890", "id": "785", "filename": "785 - 0C697705-3141-40F3-A0CD-BA6825CD629C.mov",
     "time": NOW - 3600, "reason": "timeout"},
    {"chat": "-1001234567890", "id": "801", "filename": "801 - 会议纪要_录音转写.mp4",
     "time": NOW - 7200, "reason": "FloodWait"},
]

MOCK_JS = """
<script>
(function () {
  var paused = false;
  var data = %s;
  function respond(body, type) {
    return new Response(body, { status: 200, headers: { 'content-type': type || 'application/json' } });
  }
  window.fetch = function (url, options) {
    var u = String(url);
    if (u.indexOf('/get_download_status') !== -1) {
      return Promise.resolve(respond(JSON.stringify({ download_speed: '4.28 MB/s', upload_speed: '0.00 B/s' })));
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
    if (u.indexOf('/retry_failed') !== -1 || u.indexOf('/clear_failed') !== -1) {
      return Promise.resolve(respond(JSON.stringify({ code: '1' })));
    }
    return Promise.resolve(respond('[]'));
  };
})();
</script>
""" % json.dumps(
    {"downloading": DOWNLOADING, "downloaded": DOWNLOADED, "failed": FAILED},
    ensure_ascii=False,
)


def render(name: str, state: str = "pause") -> str:
    html = (TEMPLATES / name).read_text(encoding="utf-8")
    html = URL_FOR.sub(lambda m: f"../module/static/{m.group(1)}", html)
    html = html.replace("{{ version }}", "2.2.6-fork.1")
    html = html.replace("{{ download_state }}", state)

    # Inject the mocked fetch layer before the application script runs.
    marker = '<script src="../module/static/js/app.js"></script>'
    if marker in html:
        html = html.replace(marker, MOCK_JS + marker)
    return html


def main() -> None:
    (OUT / "index.html").write_text(render("index.html"), encoding="utf-8")
    (OUT / "login.html").write_text(render("login.html"), encoding="utf-8")
    print("preview written to", OUT)


if __name__ == "__main__":
    main()
