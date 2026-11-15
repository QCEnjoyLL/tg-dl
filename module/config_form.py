"""Form schema for editing ``config.yaml`` from the Web UI.

The schema is the single source of truth for three things: what the browser
renders, how a value is parsed back, and which config path it writes to. Keeping
it in one place means the form, the server-side validation and the YAML document
can never drift apart.

Secrets are never sent to the browser: :func:`read_values` masks them and
:func:`parse_form` restores the masked placeholder from the current file.
"""

from typing import Any, Dict, List, Tuple

MASK = "********"

#: Sentinels used while parsing; a blank input means "remove this key".
UNSET = object()
KEEP = object()

MEDIA_TYPE_OPTIONS = [
    ("audio", "音频"),
    ("photo", "图片"),
    ("video", "视频"),
    ("document", "文档"),
    ("voice", "语音"),
    ("video_note", "圆视频"),
]

FILE_PATH_PREFIX_OPTIONS = [
    ("chat_title", "频道/群组名"),
    ("media_datetime", "日期目录"),
    ("media_type", "媒体类型"),
]

FILE_NAME_PREFIX_OPTIONS = [
    ("message_id", "消息 ID"),
    ("file_name", "原始文件名"),
    ("caption", "标题/文案"),
]

LANGUAGE_OPTIONS = [("EN", "English"), ("ZH", "中文"), ("RU", "Русский"), ("UA", "Українська")]
LOG_LEVEL_OPTIONS = [("DEBUG", "DEBUG"), ("INFO", "INFO"), ("WARNING", "WARNING"), ("ERROR", "ERROR")]
UPLOAD_ADAPTER_OPTIONS = [("rclone", "rclone"), ("aligo", "aligo")]

FIELDS: List[Dict[str, Any]] = [
    # ------------------------------------------------------------------ 基础
    {
        "name": "api_id",
        "path": ["api_id"],
        "label": "API ID",
        "group": "基础",
        "type": "number",
        "required": True,
        "hint": "在 my.telegram.org/apps 申请，修改后需重启。",
        "placeholder": "1234567",
    },
    {
        "name": "api_hash",
        "path": ["api_hash"],
        "label": "API Hash",
        "group": "基础",
        "type": "password",
        "required": True,
        "hint": "与 API ID 一起申请，修改后需重启。",
        "placeholder": "32 位十六进制字符串",
    },
    {
        "name": "bot_token",
        "path": ["bot_token"],
        "label": "Bot Token",
        "group": "基础",
        "type": "password",
        "hint": "留空则只做一次性下载；配置后进程常驻，可用 bot 指令和网页重试。",
        "placeholder": "123456789:AA...",
    },
    {
        "name": "chat",
        "span": 2,
        "path": ["chat"],
        "label": "下载的频道 / 群组",
        "group": "基础",
        "type": "chat_list",
        "hint": "每行一个：-1001234567890 或 @username。已有的过滤器等设置会按 chat 保留。",
        "placeholder": "-1001234567890\n@some_channel",
    },
    {
        "name": "media_types",
        "path": ["media_types"],
        "label": "下载的媒体类型",
        "group": "基础",
        "type": "multiselect",
        "required": True,
        "options": MEDIA_TYPE_OPTIONS,
        "hint": "至少选一项。",
    },
    {
        "name": "save_path",
        "path": ["save_path"],
        "label": "保存目录",
        "group": "基础",
        "type": "text",
        "hint": "留空使用默认 ./downloads（容器内 /app/downloads），修改后需重启。",
        "placeholder": "/app/downloads",
    },
    {
        "name": "language",
        "path": ["language"],
        "label": "界面语言",
        "group": "基础",
        "type": "select",
        "options": LANGUAGE_OPTIONS,
        "default": "EN",
        "hint": "影响 bot 消息的语言。",
    },
    {
        "name": "allowed_user_ids",
        "path": ["allowed_user_ids"],
        "label": "Bot 白名单",
        "group": "基础",
        "type": "lines",
        "hint": "每行一个用户或群组，可填 me。留空则只有登录账号本人可用。",
        "placeholder": "me",
    },
    # -------------------------------------------------------------- 并发与超时
    {
        "name": "max_download_task",
        "path": ["max_download_task"],
        "label": "同时下载数",
        "group": "并发与超时",
        "type": "number",
        "min": 1,
        "max": 20,
        "default": 5,
        "hint": "建议 2-3，太大会频繁触发 FloodWait。修改后需重启。",
    },
    {
        "name": "max_concurrent_transmissions",
        "path": ["max_concurrent_transmissions"],
        "label": "并发传输槽位",
        "group": "并发与超时",
        "type": "number",
        "min": 1,
        "default": None,
        "hint": "Pyrogram 层并发，默认是同时下载数 × 5。修改后需重启。",
    },
    {
        "name": "start_timeout",
        "path": ["start_timeout"],
        "label": "连接超时（秒）",
        "group": "并发与超时",
        "type": "number",
        "min": 1,
        "default": 60,
        "hint": "首次连接 Telegram 的等待时间。修改后需重启。",
    },
    {
        "name": "forward_limit",
        "path": ["forward_limit"],
        "label": "每分钟转发上限",
        "group": "并发与超时",
        "type": "number",
        "min": 1,
        "default": 33,
        "hint": "bot 转发消息的频率限制，防止 FloodWait。",
    },
    # ---------------------------------------------------------- 文件命名与格式
    {
        "name": "file_formats.audio",
        "path": ["file_formats", "audio"],
        "label": "音频格式",
        "group": "文件命名与格式",
        "type": "format_list",
        "placeholder": "all 或 mp3,flac",
        "hint": "逗号分隔扩展名，all 表示全部；留空等同于 all。",
    },
    {
        "name": "file_formats.document",
        "path": ["file_formats", "document"],
        "label": "文档格式",
        "group": "文件命名与格式",
        "type": "format_list",
        "placeholder": "all 或 pdf,epub",
        "hint": "逗号分隔扩展名，all 表示全部；留空等同于 all。",
    },
    {
        "name": "file_formats.video",
        "path": ["file_formats", "video"],
        "label": "视频格式",
        "group": "文件命名与格式",
        "type": "format_list",
        "placeholder": "all 或 mp4,mkv",
        "hint": "逗号分隔扩展名，all 表示全部；留空等同于 all。",
    },
    {
        "name": "file_path_prefix",
        "path": ["file_path_prefix"],
        "label": "目录结构",
        "group": "文件命名与格式",
        "type": "multiselect",
        "options": FILE_PATH_PREFIX_OPTIONS,
        "hint": "保存路径由这些层级依次拼接。",
    },
    {
        "name": "file_name_prefix",
        "path": ["file_name_prefix"],
        "label": "文件名组成",
        "group": "文件命名与格式",
        "type": "multiselect",
        "options": FILE_NAME_PREFIX_OPTIONS,
        "hint": "文件名由这些片段依次拼接。",
    },
    {
        "name": "file_name_prefix_split",
        "path": ["file_name_prefix_split"],
        "label": "文件名分隔符",
        "group": "文件命名与格式",
        "type": "text",
        "default": " - ",
        "hint": "片段之间使用的连接符。",
    },
    {
        "name": "date_format",
        "path": ["date_format"],
        "label": "日期目录格式",
        "group": "文件命名与格式",
        "type": "text",
        "default": "%Y_%m",
        "hint": "strftime 格式，例如 %Y_%m 生成 2026_11。",
    },
    {
        "name": "hide_file_name",
        "path": ["hide_file_name"],
        "label": "隐藏文件名",
        "group": "文件命名与格式",
        "type": "bool",
        "hint": "进度卡片里只显示扩展名，适合私密内容。",
    },
    {
        "name": "enable_download_txt",
        "path": ["enable_download_txt"],
        "label": "保存纯文本消息",
        "group": "文件命名与格式",
        "type": "bool",
        "hint": "把没有媒体的文本消息也写成 .txt 文件。",
    },
    {
        "name": "after_upload_telegram_delete",
        "path": ["after_upload_telegram_delete"],
        "label": "转发到 Telegram 后删除本地",
        "group": "文件命名与格式",
        "type": "bool",
        "default": True,
        "hint": "仅在上传成功后删除。",
    },
    {
        "name": "drop_no_audio_video",
        "path": ["drop_no_audio_video"],
        "label": "丢弃无音轨视频",
        "group": "文件命名与格式",
        "type": "bool",
        "hint": "上传前剥离没有音轨的视频。",
    },
    # ------------------------------------------------------------ 上传到网盘
    {
        "name": "upload_drive.enable_upload_file",
        "path": ["upload_drive", "enable_upload_file"],
        "label": "启用网盘上传",
        "group": "上传到网盘",
        "type": "bool",
        "hint": "下载完成后通过 rclone / aligo 上传。",
    },
    {
        "name": "upload_drive.upload_adapter",
        "path": ["upload_drive", "upload_adapter"],
        "label": "上传方式",
        "group": "上传到网盘",
        "type": "select",
        "options": UPLOAD_ADAPTER_OPTIONS,
        "default": "rclone",
    },
    {
        "name": "upload_drive.remote_dir",
        "path": ["upload_drive", "remote_dir"],
        "label": "远端目录",
        "group": "上传到网盘",
        "type": "text",
        "placeholder": "115:Telegram",
        "hint": "rclone remote 名 + 目录，例如 115:Telegram。",
    },
    {
        "name": "upload_drive.rclone_path",
        "path": ["upload_drive", "rclone_path"],
        "label": "rclone 路径",
        "group": "上传到网盘",
        "type": "text",
        "placeholder": "/app/rclone/rclone",
        "hint": "容器内默认已内置 /app/rclone/rclone。",
    },
    {
        "name": "upload_drive.before_upload_file_zip",
        "path": ["upload_drive", "before_upload_file_zip"],
        "label": "上传前压缩",
        "group": "上传到网盘",
        "type": "bool",
        "hint": "会额外生成 zip，一般建议关闭。",
    },
    {
        "name": "upload_drive.after_upload_file_delete",
        "path": ["upload_drive", "after_upload_file_delete"],
        "label": "上传成功后删除本地",
        "group": "上传到网盘",
        "type": "bool",
        "default": True,
        "hint": "rclone 返回 0 才删除；失败会保留文件。",
    },
    # ------------------------------------------------------------------ 网络
    {
        "name": "proxy.scheme",
        "path": ["proxy", "scheme"],
        "label": "代理协议",
        "group": "网络",
        "type": "text",
        "placeholder": "socks5 / http",
        "hint": "留空表示不使用代理。修改后需重启。",
    },
    {
        "name": "proxy.hostname",
        "path": ["proxy", "hostname"],
        "label": "代理地址",
        "group": "网络",
        "type": "text",
        "placeholder": "127.0.0.1",
    },
    {
        "name": "proxy.port",
        "path": ["proxy", "port"],
        "label": "代理端口",
        "group": "网络",
        "type": "number",
        "placeholder": "1080",
    },
    {
        "name": "proxy.username",
        "path": ["proxy", "username"],
        "label": "代理用户名",
        "group": "网络",
        "type": "text",
    },
    {
        "name": "proxy.password",
        "path": ["proxy", "password"],
        "label": "代理密码",
        "group": "网络",
        "type": "password",
    },
    # ------------------------------------------------------------------- Web
    {
        "name": "web_host",
        "path": ["web_host"],
        "label": "Web 监听地址",
        "group": "Web 与日志",
        "type": "text",
        "default": "0.0.0.0",
        "hint": "未设置访问密码时只会监听 127.0.0.1。修改后需重启。",
    },
    {
        "name": "web_port",
        "path": ["web_port"],
        "label": "Web 端口",
        "group": "Web 与日志",
        "type": "number",
        "min": 1,
        "max": 65535,
        "default": 5000,
        "hint": "修改后需重启。",
    },
    {
        "name": "web_login_secret",
        "path": ["web_login_secret"],
        "label": "Web 访问密码",
        "group": "Web 与日志",
        "type": "password",
        "hint": "留空则 Web UI 只监听本机；建议套 HTTPS 反向代理。支持 pbkdf2: 哈希。",
    },
    {
        "name": "log_level",
        "path": ["log_level"],
        "label": "日志级别",
        "group": "Web 与日志",
        "type": "select",
        "options": LOG_LEVEL_OPTIONS,
        "default": "INFO",
    },
    # -------------------------------------------------------------- 广告过滤
    {
        "name": "filter_advertisement_list",
        "span": 2,
        "path": ["filter_advertisement_list"],
        "label": "过滤广告关键词",
        "group": "广告过滤",
        "type": "lines",
        "hint": "每行一个关键词，命中则跳过该消息。",
    },
    {
        "name": "replace_advertisement_list",
        "span": 2,
        "path": ["replace_advertisement_list"],
        "label": "删除广告文案",
        "group": "广告过滤",
        "type": "lines",
        "hint": "每行一段文本，转发前从标题里删掉。",
    },
]

GROUPS: List[str] = []
for _field in FIELDS:
    if _field["group"] not in GROUPS:
        GROUPS.append(_field["group"])

#: Form field name -> schema entry.
BY_NAME = {field["name"]: field for field in FIELDS}


def _get_path(config: dict, path: List[str]) -> Any:
    current: Any = config
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _set_path(config: dict, path: List[str], value: Any) -> None:
    current = config
    for key in path[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[path[-1]] = value


def _del_path(config: dict, path: List[str]) -> None:
    parents: List[Tuple[dict, str]] = []
    current = config
    for key in path[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            return
        parents.append((current, key))
        current = child
    current.pop(path[-1], None)
    # Drop containers that just became empty (e.g. an all-blank proxy block).
    for parent, key in reversed(parents):
        if isinstance(parent.get(key), dict) and not parent[key]:
            parent.pop(key)


def _string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]


def parse_format_list(raw: str) -> List[str]:
    """Accept ``all``, ``mp3,flac`` and pasted YAML lists like ``['all']``."""
    text = raw.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    parts: List[str] = []
    for item in text.replace("\n", ",").split(","):
        cleaned = item.strip().strip("'\"").strip()
        if cleaned and cleaned not in parts:
            parts.append(cleaned)
    return parts


def read_values(config: dict) -> Dict[str, Any]:
    """Build the form values for the browser, masking secrets."""
    values: Dict[str, Any] = {}
    for field in FIELDS:
        name = field["name"]
        value = _get_path(config, field["path"])
        field_type = field["type"]

        if field_type == "bool":
            values[name] = bool(
                value if value is not None else field.get("default", False)
            )
        elif field_type == "multiselect":
            values[name] = _string_list(value)
        elif field_type == "lines":
            values[name] = "\n".join(_string_list(value))
        elif field_type == "chat_list":
            chats = value if isinstance(value, list) else []
            ids = []
            for item in chats:
                if isinstance(item, dict):
                    chat_id = item.get("chat_id")
                    if chat_id not in (None, ""):
                        ids.append(str(chat_id))
            # Older configs only have the legacy top-level chat_id fields.
            if not ids and config.get("chat_id") not in (None, ""):
                ids.append(str(config["chat_id"]))
            values[name] = "\n".join(ids)
        elif field_type == "format_list":
            if isinstance(value, (list, tuple)):
                values[name] = ", ".join(str(item) for item in value)
            else:
                values[name] = "" if value is None else str(value)
        elif field_type == "password":
            text = "" if value is None else str(value)
            values[name] = MASK if text else ""
        elif field_type == "number":
            values[name] = "" if value is None else str(value)
        else:
            values[name] = "" if value is None else str(value)
    return values


def _parse_number(field: Dict[str, Any], raw: str, errors: Dict[str, str]):
    try:
        number = int(raw)
    except ValueError:
        errors[field["name"]] = "请填写整数"
        return UNSET
    if field.get("min") is not None and number < field["min"]:
        errors[field["name"]] = f"不能小于 {field['min']}"
        return UNSET
    if field.get("max") is not None and number > field["max"]:
        errors[field["name"]] = f"不能大于 {field['max']}"
        return UNSET
    return number


def parse_form(form, current_config: dict) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Turn submitted form fields into ``{name: value}`` plus per-field errors.

    ``UNSET`` means "remove this key from the file"; ``KEEP`` means "leave the
    stored value alone" (the masked secret placeholder).
    """
    values: Dict[str, Any] = {}
    errors: Dict[str, str] = {}

    for field in FIELDS:
        name = field["name"]
        field_type = field["type"]
        marked = (name + "__present") in form

        if field_type == "bool":
            # The marker distinguishes "rendered and unchecked" (false) from a
            # partial post that simply did not include the field (keep as is).
            values[name] = KEEP if not marked else (name in form)
            continue

        if field_type == "multiselect":
            if not marked:
                values[name] = KEEP
                continue
            chosen = form.getlist(name)
            allowed = {option[0] for option in field.get("options", [])}
            values[name] = [item for item in chosen if item in allowed]
            if field.get("required") and not values[name]:
                errors[name] = "至少选择一项"
            continue

        if name not in form:
            values[name] = KEEP
            continue

        raw = (form.get(name) or "").strip()

        if field_type == "password":
            if raw == MASK:
                values[name] = KEEP
            else:
                values[name] = raw if raw else UNSET
            continue

        if field_type == "number":
            values[name] = UNSET if not raw else _parse_number(field, raw, errors)
            continue

        if field_type == "format_list":
            if not raw:
                values[name] = UNSET
            else:
                parsed_formats = parse_format_list(raw)
                values[name] = parsed_formats if parsed_formats else UNSET
            continue

        if field_type == "lines":
            items = [line.strip() for line in raw.splitlines() if line.strip()]
            values[name] = items if items else UNSET
            continue

        if field_type == "chat_list":
            values[name] = raw  # handled by apply_chat_list
            continue

        if field_type == "select":
            allowed = {option[0] for option in field.get("options", [])}
            if not raw:
                values[name] = UNSET
            elif raw not in allowed:
                errors[name] = "请选择有效选项"
                values[name] = UNSET
            else:
                values[name] = raw
            continue

        # text / textarea
        values[name] = raw if raw else UNSET

        if field.get("required") and values[name] is UNSET:
            errors[name] = "必填"

    return values, errors


def _coerce_chat_id(text: str):
    if text.lstrip("-").isdigit():
        return int(text)
    return text


def apply_values(config: dict, values: Dict[str, Any]) -> None:
    """Write parsed form values into the YAML document (comments are kept)."""
    old_chats = config.get("chat")
    if not isinstance(old_chats, list):
        old_chats = []

    for field in FIELDS:
        name = field["name"]
        if name not in values:
            continue
        value = values[name]

        if field["type"] == "chat_list":
            lines = [line.strip() for line in str(value).splitlines() if line.strip()]
            if not lines:
                config.pop("chat", None)
                continue
            preserved = {}
            for item in old_chats:
                if isinstance(item, dict) and "chat_id" in item:
                    preserved[str(item["chat_id"])] = item
            new_chats = []
            for line in lines:
                chat_id = _coerce_chat_id(line.split()[0])
                existing = preserved.pop(str(chat_id), None)
                if existing is not None:
                    new_chats.append(existing)
                else:
                    new_chats.append(
                        {"chat_id": chat_id, "last_read_message_id": 0}
                    )
            config["chat"] = new_chats
            continue

        if value is KEEP:
            continue

        if value is UNSET:
            _del_path(config, field["path"])
        else:
            _set_path(config, field["path"], value)


def validate(config: dict) -> Dict[str, str]:
    """Final safety checks after applying the form."""
    errors: Dict[str, str] = {}
    if not config.get("api_id"):
        errors["api_id"] = "必填"
    if not config.get("api_hash"):
        errors["api_hash"] = "必填"
    media_types = config.get("media_types")
    if not isinstance(media_types, list) or not media_types:
        errors["media_types"] = "至少选择一项"
    return errors


def schema() -> dict:
    """JSON payload describing the form for the browser."""
    return {"groups": GROUPS, "fields": FIELDS, "mask": MASK}
