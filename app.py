"""极简日程表 —— Flask + SQLite 单文件后端。

页面:
    /                  月历（点击日期进入当天）
    /day/<YYYY-MM-DD>  当天日程（事件文本框 + 截图收纳框）
    /login /logout     账号密码登录

接口:
    GET    /api/days/<date>             读取当天事件与截图
    PUT    /api/days/<date>             保存事件（JSON）
    POST   /api/days/<date>/images      粘贴上传截图（原始图片字节）
    DELETE /api/images/<id>             删除截图
    GET    /uploads/<date>/<file>       查看/下载截图（需登录）
"""

from __future__ import annotations

import calendar
import datetime as dt
import base64
import hmac
import json
import os
import posixpath
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)

BASE_DIR = Path(__file__).resolve().parent
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
UPLOAD_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(?:png|jpg|gif|webp)$")
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_EVENT_CHARS = 20000
WEEKDAY_CN = "一二三四五六日"

# 简单的内存登录限流：同一 IP 10 分钟内失败 10 次则暂时拒绝
_FAILED_LOGINS: dict[str, list[float]] = {}
_LOGIN_WINDOW = 600
_LOGIN_MAX_FAILS = 10
# AI 解析按 IP 简单限频，避免误触/并发导致费用失控
_AI_LAST_CALL: dict[str, float] = {}
_AI_MIN_INTERVAL = 2.0


def _resolve_tz() -> dt.tzinfo:
    name = os.environ.get("APP_TZ", "Asia/Shanghai")
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - 缺少时区数据时退回本机时区
        return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc


def _sniff_image(data: bytes) -> tuple[str, str] | None:
    """根据文件头识别图片类型，并做最小结构校验。

    只放行常见位图，拒绝 SVG（防 XSS）与仅有文件头、实际无法解码的假图片。
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) >= 67 and data[12:16] == b"IHDR":
            return "image/png", "png"
        return None
    if data.startswith(b"\xff\xd8\xff"):
        if len(data) >= 128 and data.endswith(b"\xff\xd9"):
            return "image/jpeg", "jpg"
        return None
    if data.startswith((b"GIF87a", b"GIF89a")):
        if len(data) >= 32 and data.endswith(b"\x3b"):
            return "image/gif", "gif"
        return None
    if len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        declared_size = int.from_bytes(data[4:8], "little") + 8
        if declared_size <= len(data):
            return "image/webp", "webp"
        return None
    return None


def _safe_next(value: str | None) -> str | None:
    """只允许站内相对路径，拒绝 //host、/\\host 等开放重定向写法。"""
    if not value or not value.startswith("/") or value.startswith("//"):
        return None
    if any(char in value for char in ("\\", "\r", "\n", "\t")):
        return None
    decoded = urllib.parse.unquote(value)
    if any(char in decoded for char in ("\\", "\r", "\n", "\t")):
        return None
    if decoded.startswith("//"):
        return None
    parts = urllib.parse.urlsplit(value)
    if parts.scheme or parts.netloc:
        return None
    path = posixpath.normpath(parts.path)
    if not path.startswith("/") or path.startswith("//"):
        return None
    if parts.path.endswith("/") and path != "/":
        path += "/"
    return path + (("?" + parts.query) if parts.query else "")


def _valid_date(value: str) -> dt.date | None:
    if not DATE_RE.match(value):
        return None
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        return None
    if not 1970 <= parsed.year <= 2100:
        return None
    return parsed


def _parse_month(value: str | None, today: dt.date) -> dt.date:
    if value and MONTH_RE.match(value):
        year, month = (int(part) for part in value.split("-"))
        if 1970 <= year <= 2100 and 1 <= month <= 12:
            return dt.date(year, month, 1)
    return today.replace(day=1)


def _shift_month(first: dt.date, delta: int) -> dt.date:
    index = first.year * 12 + (first.month - 1) + delta
    year, month = divmod(index, 12)
    return dt.date(year, month + 1, 1)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso_now() -> str:
    return _now().isoformat(timespec="seconds")


def _size_label(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{round(size / 1024)} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _extract_json_object(text: str) -> dict | None:
    """从模型输出里截取 JSON 对象（兼容 ```json 代码块）。"""
    if not isinstance(text, str):
        return None
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    start = value.find("{")
    end = value.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        payload = json.loads(value[start : end + 1])
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _normalize_ai_time(value: str) -> str:
    """把模型返回的常见时间格式统一成 HH:MM；无法确定时返回空。"""
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.translate(str.maketrans("０１２３４５６７８９：", "0123456789:"))

    # 15:00 / 15:00:00 / 15:00-16:00 / 3:00 PM / 2026-11-17 15:00
    match = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?::\d{2})?(?!\d)", text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        tail = text[match.end() : match.end() + 4].upper()
        if "PM" in tail and hour < 12:
            hour += 12
        if "AM" in tail and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"

    # 下午3点 / 上午9点30 / 晚上8点 / 中午12点
    match = re.search(
        r"(上午|下午|中午|晚上|凌晨|清晨|早上)?\s*(\d{1,2})\s*[点時时](?:\s*(\d{1,2})\s*分?)?",
        text,
    )
    if match:
        period = match.group(1) or ""
        hour = int(match.group(2))
        minute = int(match.group(3) or 0)
        if period in {"下午", "晚上"} and hour < 12:
            hour += 12
        elif period == "中午" and hour < 11:
            hour += 12
        elif period == "凌晨" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
    return ""


def _event_kind_counts(text: str) -> dict[str, int]:
    """按事件行统计当天面试 / 通用日程数量。"""
    role_hints = (
        "全栈",
        "前端",
        "后端",
        "算法",
        "开发",
        "测试",
        "工程师",
        "产品经理",
        "运营",
        "设计",
        "实习",
        "校招",
        "社招",
        "候选人",
        "HR",
        "一面",
        "二面",
        "三面",
        "终面",
        "技术面",
    )
    interview_contexts = ("面试", "视频会议", "电话", "语音")
    counts = {"interview": 0, "general": 0}
    for raw_line in str(text or "").replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        has_role = any(hint in line for hint in role_hints)
        has_context = any(hint in line for hint in interview_contexts)
        if (
            "面试" in line
            or "公司：" in line
            or "岗位：" in line
            or (has_role and has_context)
        ):
            counts["interview"] += 1
        else:
            counts["general"] += 1
    return counts


def _event_kinds(text: str) -> set[str]:
    """从事件文本判断当天包含的日程类型：面试 / 通用。"""
    return {kind for kind, count in _event_kind_counts(text).items() if count}


def _normalize_ai_events(payload: dict | None) -> list[dict]:
    """校验模型返回的 events，输出干净的面试/通用日程列表。"""
    if not isinstance(payload, dict):
        return []
    raw_events = payload.get("events")
    if not isinstance(raw_events, list):
        return []

    def clean(value, limit: int) -> str:
        return str(value or "").strip().replace("\n", " ")[:limit]

    events: list[dict] = []
    seen: set[tuple[str, str, str, str, str, str, str]] = set()
    for raw in raw_events[:20]:
        if not isinstance(raw, dict):
            continue
        date_value = _valid_date(clean(raw.get("date"), 10))
        if date_value is None:
            continue
        time_value = _normalize_ai_time(str(raw.get("time") or ""))
        company = clean(raw.get("company"), 60)
        role = clean(raw.get("role") or raw.get("position"), 60)
        title = clean(
            raw.get("title") or raw.get("summary") or raw.get("subject") or raw.get("event"),
            80,
        )
        address = clean(
            raw.get("address") or raw.get("location") or raw.get("place") or raw.get("note"),
            180,
        )

        raw_kind = clean(raw.get("kind") or raw.get("type"), 20).lower()
        if raw_kind in {"interview", "面试"}:
            kind = "interview"
        elif raw_kind in {"general", "schedule", "日程", "通用"}:
            kind = "general"
        elif company or role:
            kind = "interview"
        else:
            kind = "general"

        if kind == "interview":
            if not role:
                role = title
            if not (company or role or address):
                continue
        else:
            if not title:
                title = role or company
            if not (title or address):
                continue

        key = (
            date_value.isoformat(),
            time_value,
            kind,
            title,
            company,
            role,
            address,
        )
        if key in seen:
            continue
        seen.add(key)
        events.append(
            {
                "date": date_value.isoformat(),
                "time": time_value,
                "kind": kind,
                "title": title,
                "company": company,
                "role": role,
                "address": address,
            }
        )
    return events


def _ai_event_line(event: dict) -> str:
    parts = []
    if event.get("kind") == "general":
        if event.get("title"):
            parts.append(f"事项：{event['title']}")
        if event.get("address"):
            parts.append(f"地址：{event['address']}")
    else:
        if event.get("company"):
            parts.append(f"公司：{event['company']}")
        if event.get("role"):
            parts.append(f"岗位：{event['role']}")
        if event.get("address"):
            parts.append(f"地址：{event['address']}")
    body = "｜".join(parts)
    return f"{event.get('time', '')} {body}".strip()[:240]


def create_app(data_dir: str | Path | None = None) -> Flask:
    data_dir = Path(data_dir or os.environ.get("DATA_DIR") or BASE_DIR / "data").resolve()
    upload_dir = data_dir / "uploads"
    db_path = data_dir / "app.db"
    data_dir.mkdir(parents=True, exist_ok=True)
    upload_dir.mkdir(parents=True, exist_ok=True)

    secret_key = os.environ.get("SECRET_KEY", "").strip()
    if not secret_key:
        secret_file = data_dir / "secret_key"
        if secret_file.exists():
            secret_key = secret_file.read_text(encoding="utf-8").strip()
        if not secret_key:
            secret_key = secrets.token_hex(32)
            secret_file.write_text(secret_key, encoding="utf-8")
            try:
                os.chmod(secret_file, 0o600)
            except OSError:
                pass

    tz = _resolve_tz()
    today_fn = lambda: dt.datetime.now(tz).date()  # noqa: E731

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secret_key,
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES + 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "") == "1",
        PERMANENT_SESSION_LIFETIME=dt.timedelta(days=30),
        APP_USERNAME=os.environ.get("APP_USERNAME", "admin"),
        APP_PASSWORD=os.environ.get("APP_PASSWORD", "admin123"),
        VISION_API_KEY=os.environ.get("DASHSCOPE_API_KEY", "").strip(),
        VISION_API_URL=os.environ.get(
            "VISION_API_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        ).strip(),
        VISION_MODEL=os.environ.get("VISION_MODEL", "qwen3-vl-plus").strip(),
        CHAT_MODEL=os.environ.get(
            "CHAT_MODEL", os.environ.get("VISION_MODEL", "qwen3-vl-plus")
        ).strip(),
        DATA_DIR=data_dir,
        UPLOAD_DIR=upload_dir,
        DB_PATH=db_path,
    )
    app.json.ensure_ascii = False

    def get_db() -> sqlite3.Connection:
        if "db" not in g:
            connection = sqlite3.connect(db_path)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            g.db = connection
        return g.db

    def init_db() -> None:
        connection = sqlite3.connect(db_path)
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS days (
                    date       TEXT PRIMARY KEY,
                    event      TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS images (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    date       TEXT NOT NULL,
                    slot       INTEGER NOT NULL DEFAULT 0,
                    filename   TEXT NOT NULL,
                    mime       TEXT NOT NULL,
                    size       INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_images_date ON images(date);
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    role       TEXT NOT NULL,
                    content    TEXT NOT NULL,
                    changes    TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chat_messages_id ON chat_messages(id);
                """
            )
            # 兼容旧库：早期版本没有 slot 字段，所有截图默认归到第一个 <资料>
            columns = [row[1] for row in connection.execute("PRAGMA table_info(images)")]
            if "slot" not in columns:
                connection.execute(
                    "ALTER TABLE images ADD COLUMN slot INTEGER NOT NULL DEFAULT 0"
                )
            connection.commit()
        finally:
            connection.close()

    init_db()

    @app.teardown_appcontext
    def _close_db(_exception: BaseException | None) -> None:
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("user"):
                if request.path.startswith(("/api/", "/uploads/")):
                    return jsonify({"error": "unauthorized"}), 401
                next_url = request.script_root + request.full_path
                return redirect(url_for("login", next=next_url.rstrip("?")))
            return view(*args, **kwargs)

        return wrapped

    def is_throttled(ip: str) -> bool:
        now = time.time()
        recent = [stamp for stamp in _FAILED_LOGINS.get(ip, []) if now - stamp < _LOGIN_WINDOW]
        _FAILED_LOGINS[ip] = recent
        if not recent:
            _FAILED_LOGINS.pop(ip, None)
        return len(recent) >= _LOGIN_MAX_FAILS

    def record_failure(ip: str) -> None:
        _FAILED_LOGINS.setdefault(ip, []).append(time.time())

    def hhmm(iso: str | None) -> str:
        if not iso:
            return ""
        try:
            value = dt.datetime.fromisoformat(iso)
        except ValueError:
            return ""
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(tz).strftime("%H:%M")

    def image_json(row: sqlite3.Row) -> dict:
        url = url_for("uploaded_file", date_str=row["date"], name=row["filename"])
        return {
            "id": row["id"],
            "date": row["date"],
            "slot": row["slot"],
            "url": url,
            "download_url": url + "?download=1",
            "mime": row["mime"],
            "size": row["size"],
            "size_label": _size_label(row["size"]),
            "created_at": row["created_at"],
            "time_label": hhmm(row["created_at"]),
        }

    def images_for(date_iso: str) -> list[sqlite3.Row]:
        return get_db().execute(
            "SELECT * FROM images WHERE date = ? ORDER BY created_at ASC, id ASC",
            (date_iso,),
        ).fetchall()

    def vision_parse(image_bytes: bytes, mime: str) -> list[dict]:
        """调用视觉模型解析截图，返回校验后的日程列表。"""
        api_key = str(app.config["VISION_API_KEY"])
        if not api_key:
            raise RuntimeError("AI 识别服务未配置")
        now = dt.datetime.now(tz)
        weekday = WEEKDAY_CN[now.weekday()]
        tz_name = os.environ.get("APP_TZ", "Asia/Shanghai")
        prompt = f"""你是日程截图解析助手。当前时间：{now.strftime('%Y-%m-%d %H:%M')}（{tz_name}，星期{weekday}）。
截图可能来自聊天记录、招聘软件、邮件或日历。请提取所有对用户有效的日程，包括面试，以及会议、约见、课程、出行、缴费、纪念日、待办等通用日程。
只输出严格 JSON，不要 markdown，不要解释：
{{"events":[
  {{"kind":"interview","date":"YYYY-MM-DD","time":"HH:MM 或空","company":"公司名","role":"岗位/职位","address":"地址或线上会议信息"}},
  {{"kind":"general","date":"YYYY-MM-DD","time":"HH:MM 或空","title":"事项","address":"地点或线上会议信息"}}
]}}
规则：
1. date 用截图里能确定的绝对日期；相对日期（今天/明天/后天/下周一/本周五等）按当前时间换算成具体日期；截图完全没有日期时不要输出这一条。
2. time 必须是截图中真实出现的时间，用 24 小时制 HH:MM；时间段取开始时间（如 15:00-16:00 记 15:00）；截图只有日期、没有具体时间时留空；绝对不要用当前时间填充。
3. kind：面试/招聘相关填 interview，其他日程填 general；即使截图没有「面试」二字，只要出现候选人/面试官、岗位 + 视频会议/电话沟通、技术面/HR 沟通等，也按 interview；同一张截图里两类都有时都要输出，不要只提取面试。
4. interview：company 只填公司名，role 填岗位/职位，address 填面试地址或线上会议信息；只填截图里明确出现的信息，不确定就留空，不要用部门、人名或会议软件名冒充公司名。
5. general：title 填简短事项，例如「产品分享会」「取快递」「交房租」「体检」；address 填地点、链接或会议号；company 和 role 留空。
6. 同一日程只输出一次；截图里没有明确日程时输出 {{"events":[]}}。"""
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "model": str(app.config["VISION_MODEL"]),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{image_b64}"},
                        },
                    ],
                }
            ],
            "temperature": 0,
        }
        request_obj = urllib.request.Request(
            str(app.config["VISION_API_URL"]),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request_obj, timeout=90) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"AI 服务返回错误（HTTP {exc.code}）：{detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"AI 服务连接失败：{exc}") from None

        try:
            data = json.loads(raw)
        except ValueError:
            raise RuntimeError("AI 返回格式异常") from None
        choices = data.get("choices") or []
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("AI 没有返回识别结果")
        content = (choices[0].get("message") or {}).get("content", "")
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        return _normalize_ai_events(_extract_json_object(str(content)))

    def chat_snapshot() -> str:
        """给对话模型提供最近 120 天的逐行日程快照。"""
        today = today_fn()
        rows = get_db().execute(
            "SELECT date, event FROM days WHERE date BETWEEN ? AND ? ORDER BY date",
            (
                (today - dt.timedelta(days=7)).isoformat(),
                (today + dt.timedelta(days=120)).isoformat(),
            ),
        ).fetchall()
        lines = []
        for row in rows:
            text = (row["event"] or "").replace("\r\n", "\n").strip()
            if not text:
                continue
            lines.append(f"{row['date']}:")
            for item in text.split("\n"):
                item = item.strip()
                if item:
                    lines.append(f"  - {item[:500]}")
        return "\n".join(lines)[:8000] or "（未来 120 天暂无日程）"

    def load_chat_history(limit: int = 40) -> list[dict]:
        """读取最近对话，返回 role/content/changes；用于页面恢复和模型上下文。"""
        rows = get_db().execute(
            "SELECT role, content, changes FROM chat_messages ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        messages = []
        for row in reversed(rows):
            try:
                changes = json.loads(row["changes"] or "[]")
            except (TypeError, ValueError):
                changes = []
            if not isinstance(changes, list):
                changes = []
            messages.append(
                {
                    "role": row["role"],
                    "content": row["content"],
                    "changes": changes,
                }
            )
        return messages

    def save_chat_turn(user_content: str, reply: str, changes: list[dict]) -> None:
        """把一轮对话写入 SQLite，并只保留最近 200 条。"""
        db = get_db()
        now_iso = _iso_now()
        db.execute(
            "INSERT INTO chat_messages(role, content, changes, created_at) VALUES (?, ?, ?, ?)",
            ("user", user_content[:2000], "[]", now_iso),
        )
        db.execute(
            "INSERT INTO chat_messages(role, content, changes, created_at) VALUES (?, ?, ?, ?)",
            (
                "assistant",
                reply[:4000],
                json.dumps(changes[:20], ensure_ascii=False),
                now_iso,
            ),
        )
        db.execute(
            """
            DELETE FROM chat_messages
            WHERE id NOT IN (SELECT id FROM chat_messages ORDER BY id DESC LIMIT 200)
            """
        )
        db.commit()

    def apply_ai_events(events: list[dict]) -> tuple[list[dict], list[dict]]:
        """把校验后的日程追加到事件文本，跳过已存在的行。"""
        db = get_db()
        added: list[dict] = []
        skipped: list[dict] = []
        now_iso = _iso_now()
        for event in events:
            line = _ai_event_line(event)
            row = db.execute(
                "SELECT event FROM days WHERE date = ?", (event["date"],)
            ).fetchone()
            existing = row["event"] if row else ""
            existing_lines = {item.strip() for item in existing.splitlines() if item.strip()}
            if line in existing_lines:
                skipped.append({**event, "line": line, "reason": "已存在"})
                continue
            new_event = existing.rstrip("\n")
            new_event = (new_event + "\n" if new_event else "") + line
            db.execute(
                """
                INSERT INTO days(date, event, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET event = excluded.event, updated_at = excluded.updated_at
                """,
                (event["date"], new_event, now_iso),
            )
            added.append({**event, "line": line})
        db.commit()
        return added, skipped

    def apply_chat_actions(actions: list[dict]) -> dict:
        """执行对话模型的 create/update/move/delete/shift 操作。"""
        db = get_db()
        lines_by_date: dict[str, list[str]] = {}
        for row in db.execute("SELECT date, event FROM days"):
            lines_by_date[row["date"]] = [
                line.strip()
                for line in (row["event"] or "").replace("\r\n", "\n").split("\n")
                if line.strip()
            ]

        dirty: set[str] = set()
        pending: list[tuple[str, str]] = []
        pending_seen: set[tuple[str, str]] = set()
        changes: list[dict] = []
        failed: list[dict] = []
        added: list[dict] = []
        skipped: list[dict] = []

        def clean_line(value) -> str:
            return str(value or "").strip().replace("\r", "").replace("\n", " ")[:500]

        def fail(reason: str, action: dict) -> None:
            failed.append({"reason": reason, "action": action})

        def take_line(date: str, line: str) -> bool:
            items = lines_by_date.setdefault(date, [])
            if line not in items:
                return False
            items.remove(line)
            dirty.add(date)
            return True

        def looks_like_meeting(line: str) -> bool:
            return any(
                marker in line
                for marker in ("面试", "会议", "公司：", "岗位：", "http://", "https://")
            )

        for action in actions[:20]:
            if not isinstance(action, dict):
                continue
            action_type = str(action.get("type") or "create").lower()

            if action_type == "create":
                events = _normalize_ai_events({"events": [action]})
                if not events:
                    fail("缺少有效的日期或事项", action)
                    continue
                event = events[0]
                line = _ai_event_line(event)
                date = event["date"]
                key = (date, line)
                if line in lines_by_date.get(date, []) or key in pending_seen:
                    skipped.append({**event, "line": line, "reason": "已存在"})
                    continue
                pending.append(key)
                pending_seen.add(key)
                dirty.add(date)
                added.append({**event, "line": line})
                changes.append({"type": "create", "date": date, "line": line})
                continue

            if action_type == "update":
                date = _valid_date(str(action.get("date") or "").strip())
                new_date = _valid_date(
                    str(action.get("new_date") or (date.isoformat() if date else "")).strip()
                )
                old_line = clean_line(action.get("old_line"))
                new_line = clean_line(action.get("new_line"))
                if not date or not new_date or not old_line or not new_line:
                    fail("修改需要 date、old_line、new_line", action)
                    continue
                if not take_line(date.isoformat(), old_line):
                    fail("找不到要修改的原日程行", action)
                    continue
                pending.append((new_date.isoformat(), new_line))
                dirty.add(new_date.isoformat())
                changes.append(
                    {
                        "type": "update",
                        "date": new_date.isoformat(),
                        "from_date": date.isoformat(),
                        "old_line": old_line,
                        "line": new_line,
                    }
                )
                continue

            if action_type == "move":
                from_date = _valid_date(str(action.get("from_date") or "").strip())
                to_date = _valid_date(str(action.get("to_date") or "").strip())
                line = clean_line(action.get("line"))
                new_line = clean_line(action.get("new_line") or line)
                if not from_date or not to_date or not line:
                    fail("移动需要 from_date、line、to_date", action)
                    continue
                if not take_line(from_date.isoformat(), line):
                    fail("找不到要移动的原日程行", action)
                    continue
                pending.append((to_date.isoformat(), new_line))
                dirty.add(to_date.isoformat())
                changes.append(
                    {
                        "type": "move",
                        "date": to_date.isoformat(),
                        "from_date": from_date.isoformat(),
                        "line": new_line,
                        "old_line": line,
                    }
                )
                continue

            if action_type == "delete":
                date = _valid_date(str(action.get("date") or "").strip())
                line = clean_line(action.get("line"))
                if not date or not line:
                    fail("删除需要 date、line", action)
                    continue
                if not take_line(date.isoformat(), line):
                    fail("找不到要删除的日程行", action)
                    continue
                changes.append(
                    {"type": "delete", "date": date.isoformat(), "line": line}
                )
                continue

            if action_type == "shift":
                after_date = _valid_date(str(action.get("after_date") or "").strip())
                try:
                    days = int(action.get("days") or 0)
                except (TypeError, ValueError):
                    days = 0
                scope = str(action.get("scope") or "general").lower()
                if scope not in {"general", "all"}:
                    scope = "general"
                if not after_date or days == 0 or abs(days) > 30:
                    fail("整体后移需要 after_date 和 1–30 天", action)
                    continue
                selected: list[tuple[str, str]] = []
                for date_text in sorted(lines_by_date):
                    if date_text <= after_date.isoformat():
                        continue
                    for item in list(lines_by_date[date_text]):
                        if scope == "general" and looks_like_meeting(item):
                            continue
                        counts = _event_kind_counts(item)
                        if scope == "all" or counts["general"] > 0:
                            selected.append((date_text, item))
                if not selected:
                    fail("该日期之后没有可移动的日程", action)
                    continue
                for date_text, item in selected:
                    lines_by_date[date_text].remove(item)
                    dirty.add(date_text)
                for date_text, item in selected:
                    target = (
                        dt.date.fromisoformat(date_text) + dt.timedelta(days=days)
                    ).isoformat()
                    pending.append((target, item))
                    dirty.add(target)
                changes.append(
                    {
                        "type": "shift",
                        "date": after_date.isoformat(),
                        "days": days,
                        "scope": scope,
                        "count": len(selected),
                    }
                )
                continue

            if action_type == "insert":
                date = _valid_date(str(action.get("date") or "").strip())
                new_line = clean_line(action.get("line"))
                try:
                    days = int(action.get("shift") or 1)
                except (TypeError, ValueError):
                    days = 1
                if not date or not new_line or days == 0 or abs(days) > 30:
                    fail("插入需要 date、line 和 1–30 天的顺延", action)
                    continue
                date_text = date.isoformat()
                if new_line in lines_by_date.get(date_text, []):
                    fail("该日期已有相同内容", action)
                    continue
                selected: list[tuple[str, str]] = []
                for source_date in sorted(lines_by_date):
                    if source_date < date_text:
                        continue
                    for item in list(lines_by_date[source_date]):
                        if looks_like_meeting(item):
                            continue
                        selected.append((source_date, item))
                for source_date, item in selected:
                    lines_by_date[source_date].remove(item)
                    dirty.add(source_date)
                for source_date, item in selected:
                    target = (
                        dt.date.fromisoformat(source_date) + dt.timedelta(days=days)
                    ).isoformat()
                    pending.append((target, item))
                    dirty.add(target)
                pending.append((date_text, new_line))
                dirty.add(date_text)
                changes.append(
                    {
                        "type": "insert",
                        "date": date_text,
                        "line": new_line,
                        "count": len(selected),
                        "days": days,
                    }
                )
                continue

            fail("不支持的操作类型", action)

        for date_text, line in pending:
            items = lines_by_date.setdefault(date_text, [])
            if line not in items:
                items.append(line)
            dirty.add(date_text)

        now_iso = _iso_now()
        for date_text in dirty:
            event = "\n".join(lines_by_date.get(date_text, [])).strip()
            db.execute(
                """
                INSERT INTO days(date, event, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET event = excluded.event, updated_at = excluded.updated_at
                """,
                (date_text, event, now_iso),
            )
        db.commit()

        image_dates = {
            row["date"]
            for row in db.execute("SELECT DISTINCT date FROM images")
        }
        dots = {}
        for date_text in dirty:
            event = "\n".join(lines_by_date.get(date_text, [])).strip()
            counts = (
                _event_kind_counts(event)
                if event
                else {"interview": 0, "general": 0}
            )
            if not event and date_text in image_dates:
                counts["general"] = 1
            dots[date_text] = counts

        return {
            "added": added,
            "skipped": skipped,
            "changes": changes,
            "failed": failed,
            "dots": dots,
        }

    def detect_insert_request(message: str) -> tuple[str, str] | None:
        """识别“某日改成X，其他往后推/顺延”的固定意图。"""
        if not re.search(r"(往后推|后推|往后顺延|顺延)", message):
            return None
        match = re.search(
            r"(?:(\d{4})年)?\s*(?:(\d{1,2})月)?\s*(\d{1,2})\s*[日号]\s*"
            r"(?:改成|改为|换成|变成)\s*(.+)",
            message,
        )
        if not match:
            return None
        today = today_fn()
        year = int(match.group(1) or today.year)
        month = int(match.group(2) or today.month)
        day = int(match.group(3))
        rest = match.group(4).strip()
        line = re.split(r"[，,。；;]|其他", rest, maxsplit=1)[0].strip(" \t\"'“”‘’")
        if not line or len(line) > 200:
            return None
        try:
            date_value = dt.date(year, month, day)
        except ValueError:
            return None
        return date_value.isoformat(), line

    def chat_reply(
        message: str,
        history: list[dict],
        image_bytes: bytes | None,
        image_mime: str | None,
    ) -> tuple[str, list[dict]]:
        """调用对话模型，返回回复文本和待执行的排期 actions。"""
        api_key = str(app.config["VISION_API_KEY"])
        if not api_key:
            raise RuntimeError("AI 服务未配置")
        now = dt.datetime.now(tz)
        weekday = WEEKDAY_CN[now.weekday()]
        tz_name = os.environ.get("APP_TZ", "Asia/Shanghai")
        snapshot = chat_snapshot()
        system_prompt = f"""你是「日程」应用里的 AI 助手，帮用户查期、排期和改期。当前时间：{now.strftime('%Y-%m-%d %H:%M')}（{tz_name}，星期{weekday}）。
最近 120 天已有日程（日期下一行一条，行首 - 后面是当天事件原文）：
{snapshot}

你可以：
1. 查期：根据上面已有日程，回答某天、某时间段是否有空、有哪些安排。没有记录就说空闲；不要编造不存在的日程。
2. 排期/改期：用户明确要求新增、修改、移动、删除或整体后移日程时，输出 actions；缺少日期、时间、事项等关键信息时先在 reply 里追问，不要猜。

只输出严格 JSON，不要 markdown，不要解释：
{{"reply":"给用户看的中文回复","actions":[
  {{"type":"create","date":"YYYY-MM-DD","time":"HH:MM 或空","kind":"interview","company":"公司名","role":"岗位","address":"地址或会议信息"}},
  {{"type":"create","date":"YYYY-MM-DD","time":"HH:MM 或空","kind":"general","title":"事项","address":"地点或会议信息"}},
  {{"type":"update","date":"YYYY-MM-DD","old_line":"快照里的原文","new_date":"YYYY-MM-DD","new_line":"修改后的内容"}},
  {{"type":"move","from_date":"YYYY-MM-DD","line":"快照里的原文","to_date":"YYYY-MM-DD","new_line":"移动后的内容，可省略"}},
  {{"type":"delete","date":"YYYY-MM-DD","line":"快照里的原文"}},
  {{"type":"shift","after_date":"YYYY-MM-DD","days":1,"scope":"general"}},
  {{"type":"insert","date":"YYYY-MM-DD","line":"新的日程内容","shift":1}}
]}}

规则：
1. 只有用户明确要求新增/修改/移动/删除/整体后移时才输出 actions；只是查期、提问或聊天时 actions 为空。
2. date 必须是绝对日期，相对日期按当前时间换算；time 用 24 小时制 HH:MM，不确定就留空；不要用当前时间填充用户没说的时间。
3. create：面试/招聘相关 kind=interview，填 company、role、address；其他日程 kind=general，填 title、address，company/role 留空；已存在的日程不要重复 create。
4. update：修改某条已有日程的内容、时间或日期；old_line 必须与快照里某一行完全一致（去掉首尾空格），new_line 是修改后的整行内容。
5. move：把某条已有日程移到另一天；from_date、line 必须与快照完全对应，to_date 是目标日期。
6. delete：删除某条已有日程；line 必须与快照里的原文完全一致。
7. shift：把 after_date 之后的所有通用日程整体平移 days 天；scope=general 时会跳过会议/面试；这是“其他往后推”的首选，不要用 create 复制。
8. insert：在 date 插入一条新日程 line，并把从该日起的原有通用学习任务整体顺延 shift 天，会议/面试保持不动；用于“X日改成Y，其他往后推”这类插入+顺延需求，不要用 update 覆盖原任务。若用户只说“把X日改成Y”而没有“往后推”，才用 update 直接替换。
9. 修改/移动/删除前先确认 old_line/line 在快照中存在；找不到或不确定就追问，不要编造原文。用户没要求改的面试/会议不要动。
10. 用户发截图时：解析截图里明确出现的日程；如果用户只是询问截图内容，只回答不写入；如果用户要求导入，或只发截图没有提问，则生成 create actions。
11. reply 要简洁、自然、中文，说明做了哪些修改、移动了几条；不要提到 JSON 或内部规则；使用纯文本，不要 markdown 格式。"""

        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        for item in history[-12:]:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            content = str(item.get("content") or "").strip()[:2000]
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})

        if image_bytes:
            content = message or "请解析这张截图里的日程并加入日历。"
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": content},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{image_mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
                            },
                        },
                    ],
                }
            )
        else:
            messages.append({"role": "user", "content": message})

        payload = {
            "model": str(app.config["CHAT_MODEL"]),
            "messages": messages,
            "temperature": 0.2,
        }
        request_obj = urllib.request.Request(
            str(app.config["VISION_API_URL"]),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request_obj, timeout=90) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"AI 服务返回错误（HTTP {exc.code}）：{detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"AI 服务连接失败：{exc}") from None

        try:
            data = json.loads(raw)
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        except (ValueError, IndexError, AttributeError):
            raise RuntimeError("AI 返回格式异常") from None
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        text = str(content or "").strip()
        parsed = _extract_json_object(text)
        if isinstance(parsed, dict):
            reply = str(parsed.get("reply") or "").strip()
            actions = parsed.get("actions")
            if not isinstance(actions, list):
                actions = []
        else:
            reply = text
            actions = []
        if not reply:
            reply = "我暂时没有理解，可以再说一次吗？"
        return reply[:2000], [item for item in actions[:20] if isinstance(item, dict)]

    @app.get("/favicon.ico")
    def favicon():
        # 模板不引入图标资源，返回空响应避免浏览器 404
        return "", 204

    @app.get("/login")
    def login():
        if session.get("user"):
            return redirect(url_for("calendar_view"))
        return render_template("login.html", error=None, next=request.args.get("next", ""))

    @app.post("/login")
    def login_post():
        ip = request.remote_addr or "unknown"
        if is_throttled(ip):
            return render_template(
                "login.html",
                error="尝试次数过多，请 10 分钟后再试。",
                next=request.form.get("next", ""),
            ), 429
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        valid_user = hmac.compare_digest(username, str(app.config["APP_USERNAME"]))
        valid_pass = hmac.compare_digest(password, str(app.config["APP_PASSWORD"]))
        if not (valid_user and valid_pass):
            record_failure(ip)
            return render_template(
                "login.html",
                error="账号或密码不正确。",
                next=request.form.get("next", ""),
            ), 401
        _FAILED_LOGINS.pop(ip, None)
        session.clear()
        session.permanent = True
        session["user"] = username
        nxt = _safe_next(request.form.get("next")) or url_for("calendar_view")
        return redirect(nxt)

    @app.post("/logout")
    @login_required
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def calendar_view():
        today = today_fn()
        month_first = _parse_month(request.args.get("month"), today)
        prev_month = _shift_month(month_first, -1)
        next_month = _shift_month(month_first, 1)
        last_day = calendar.monthrange(month_first.year, month_first.month)[1]
        month_last = month_first.replace(day=last_day)

        db = get_db()
        start_iso, end_iso = month_first.isoformat(), month_last.isoformat()
        day_counts: dict[str, dict[str, int]] = {}
        for row in db.execute(
            "SELECT date, event FROM days WHERE date BETWEEN ? AND ?",
            (start_iso, end_iso),
        ):
            text = row["event"] or ""
            if text.strip():
                day_counts[row["date"]] = _event_kind_counts(text)
        for row in db.execute(
            "SELECT DISTINCT date FROM images WHERE date BETWEEN ? AND ?",
            (start_iso, end_iso),
        ):
            # 只有截图、没有事件文本的日期按通用日程显示一个白点
            day_counts.setdefault(row["date"], {"interview": 0, "general": 1})

        grid_start = month_first - dt.timedelta(days=month_first.weekday())
        grid_end = month_last + dt.timedelta(days=6 - month_last.weekday())
        weeks = []
        cursor = grid_start
        while cursor <= grid_end:
            week = []
            for _ in range(7):
                counts = day_counts.get(
                    cursor.isoformat(), {"interview": 0, "general": 0}
                )
                week.append(
                    {
                        "iso": cursor.isoformat(),
                        "day": cursor.day,
                        "in_month": cursor.month == month_first.month,
                        "is_today": cursor == today,
                        "has_content": counts["interview"] + counts["general"] > 0,
                        "interview_count": counts["interview"],
                        "general_count": counts["general"],
                    }
                )
                cursor += dt.timedelta(days=1)
            weeks.append(week)

        return render_template(
            "calendar.html",
            title=f"{month_first.year}年{month_first.month}月",
            month_key=month_first.strftime("%Y-%m"),
            prev_month=prev_month.strftime("%Y-%m"),
            next_month=next_month.strftime("%Y-%m"),
            weeks=weeks,
            today_iso=today.isoformat(),
            weekday_names=list(WEEKDAY_CN),
        )

    @app.get("/day/<date_str>")
    @login_required
    def day_view(date_str: str):
        day = _valid_date(date_str)
        if day is None:
            abort(404)
        db = get_db()
        row = db.execute("SELECT * FROM days WHERE date = ?", (date_str,)).fetchone()
        event = row["event"] if row else ""
        images = images_for(date_str)
        return render_template(
            "day.html",
            title=f"{day.month}月{day.day}日",
            day=day,
            date_iso=date_str,
            month_key=day.strftime("%Y-%m"),
            weekday=WEEKDAY_CN[day.weekday()],
            event=event,
            images=[image_json(item) for item in images],
        )

    @app.get("/api/days/<date_str>")
    @login_required
    def api_get_day(date_str: str):
        if _valid_date(date_str) is None:
            return jsonify({"error": "无效日期"}), 404
        row = get_db().execute("SELECT * FROM days WHERE date = ?", (date_str,)).fetchone()
        return jsonify(
            {
                "date": date_str,
                "event": row["event"] if row else "",
                "updated_at": row["updated_at"] if row else None,
                "images": [image_json(item) for item in images_for(date_str)],
            }
        )

    @app.put("/api/days/<date_str>")
    @login_required
    def api_put_day(date_str: str):
        if _valid_date(date_str) is None:
            return jsonify({"error": "无效日期"}), 404
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or "event" not in payload:
            return jsonify({"error": "请求格式错误"}), 400
        event = payload["event"]
        if not isinstance(event, str):
            return jsonify({"error": "事件内容必须是文本"}), 400
        if len(event) > MAX_EVENT_CHARS:
            return jsonify({"error": f"事件最多 {MAX_EVENT_CHARS} 字"}), 400
        now = _iso_now()
        db = get_db()
        db.execute(
            """
            INSERT INTO days(date, event, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET event = excluded.event, updated_at = excluded.updated_at
            """,
            (date_str, event, now),
        )
        db.commit()
        return jsonify({"ok": True, "updated_at": now, "time_label": hhmm(now)})

    @app.post("/api/days/<date_str>/images")
    @login_required
    def api_upload_image(date_str: str):
        if _valid_date(date_str) is None:
            return jsonify({"error": "无效日期"}), 404
        slot_raw = request.args.get("slot", "0")
        if not slot_raw.isdigit() or not 0 <= int(slot_raw) <= 99:
            return jsonify({"error": "资料位置无效"}), 400
        slot = int(slot_raw)
        if request.content_length is not None and request.content_length > MAX_UPLOAD_BYTES:
            return jsonify({"error": "图片不能超过 10MB"}), 413
        data = request.get_data(cache=False)
        if not data:
            return jsonify({"error": "没有收到图片数据"}), 400
        if len(data) > MAX_UPLOAD_BYTES:
            return jsonify({"error": "图片不能超过 10MB"}), 413
        sniffed = _sniff_image(data)
        if sniffed is None:
            return (
                jsonify({"error": "仅支持 PNG / JPG / GIF / WebP 图片"}),
                415,
            )
        mime, ext = sniffed
        filename = f"{uuid.uuid4().hex}.{ext}"
        target_dir = upload_dir / date_str
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / filename).write_bytes(data)

        created_at = _iso_now()
        db = get_db()
        cursor = db.execute(
            "INSERT INTO images(date, slot, filename, mime, size, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (date_str, slot, filename, mime, len(data), created_at),
        )
        db.commit()
        row = db.execute("SELECT * FROM images WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return jsonify({"image": image_json(row)}), 201

    @app.delete("/api/images/<int:image_id>")
    @login_required
    def api_delete_image(image_id: int):
        db = get_db()
        row = db.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
        if row is None:
            return jsonify({"error": "截图不存在"}), 404
        file_path = upload_dir / row["date"] / row["filename"]
        try:
            file_path.unlink(missing_ok=True)
        except OSError:
            app.logger.warning("删除截图文件失败: %s", file_path)
        db.execute("DELETE FROM images WHERE id = ?", (image_id,))
        db.commit()
        return jsonify({"ok": True})

    @app.post("/api/ai/parse")
    @login_required
    def api_ai_parse():
        """上传一张日程截图，调用视觉模型识别后自动写入对应日期的事件。"""
        if not app.config["VISION_API_KEY"]:
            return jsonify({"error": "AI 识别服务未配置（缺少 DASHSCOPE_API_KEY）"}), 503
        ip = request.remote_addr or "unknown"
        now_ts = time.time()
        if now_ts - _AI_LAST_CALL.get(ip, 0.0) < _AI_MIN_INTERVAL:
            return jsonify({"error": "请求太快，请稍等几秒再试"}), 429
        _AI_LAST_CALL[ip] = now_ts
        if request.content_length is not None and request.content_length > MAX_UPLOAD_BYTES:
            return jsonify({"error": "截图不能超过 10MB"}), 413
        data = request.get_data(cache=False)
        if not data:
            return jsonify({"error": "没有收到截图数据"}), 400
        if len(data) > MAX_UPLOAD_BYTES:
            return jsonify({"error": "截图不能超过 10MB"}), 413
        sniffed = _sniff_image(data)
        if sniffed is None:
            return jsonify({"error": "仅支持 PNG / JPG / GIF / WebP 图片"}), 415

        try:
            events = vision_parse(data, sniffed[0])
        except RuntimeError as exc:
            app.logger.warning("AI parse failed: %s", exc)
            return jsonify({"error": str(exc)}), 502

        added, skipped = apply_ai_events(events)
        return jsonify({"added": added, "skipped": skipped, "events": events})

    @app.get("/api/ai/chat/history")
    @login_required
    def api_ai_chat_history():
        return jsonify({"messages": load_chat_history(40)})

    @app.post("/api/ai/chat")
    @login_required
    def api_ai_chat():
        """对话式 AI：查期、排期、改期，并可附带截图解析。"""
        if not app.config["VISION_API_KEY"]:
            return jsonify({"error": "AI 服务未配置（缺少 DASHSCOPE_API_KEY）"}), 503
        ip = request.remote_addr or "unknown"
        now_ts = time.time()
        if now_ts - _AI_LAST_CALL.get(ip, 0.0) < _AI_MIN_INTERVAL:
            return jsonify({"error": "请求太快，请稍等几秒再试"}), 429
        _AI_LAST_CALL[ip] = now_ts

        message = str(request.form.get("message") or "").strip()[:2000]
        image_bytes: bytes | None = None
        image_mime: str | None = None
        image_file = request.files.get("image")
        if image_file is not None:
            image_bytes = image_file.read()
            if len(image_bytes) > MAX_UPLOAD_BYTES:
                return jsonify({"error": "截图不能超过 10MB"}), 413
            sniffed = _sniff_image(image_bytes)
            if sniffed is None:
                return jsonify({"error": "仅支持 PNG / JPG / GIF / WebP 图片"}), 415
            image_mime = sniffed[0]
        if not message and not image_bytes:
            return jsonify({"error": "请输入内容或附上一张截图"}), 400

        # 优先使用服务端保存的对话记忆；旧页面没有历史时兼容客户端传来的 history。
        history = [
            {"role": item["role"], "content": item["content"]}
            for item in load_chat_history(12)
        ]
        if not history:
            raw_history = request.form.get("history") or "[]"
            try:
                parsed_history = json.loads(raw_history)
            except ValueError:
                parsed_history = []
            if isinstance(parsed_history, list):
                for item in parsed_history[-12:]:
                    if not isinstance(item, dict):
                        continue
                    role = item.get("role")
                    content = str(item.get("content") or "").strip()
                    if role in {"user", "assistant"} and content:
                        history.append({"role": role, "content": content[:2000]})

        try:
            reply, raw_actions = chat_reply(
                message, history, image_bytes, image_mime
            )
        except RuntimeError as exc:
            app.logger.warning("AI chat failed: %s", exc)
            return jsonify({"error": str(exc)}), 502

        forced = detect_insert_request(message)
        if forced:
            forced_date, forced_line = forced
            target = _valid_date(forced_date)
            assert target is not None
            raw_actions = [
                {
                    "type": "insert",
                    "date": forced_date,
                    "line": forced_line,
                    "shift": 1,
                }
            ]
            reply = (
                f"已把 {target.month}月{target.day}日改为「{forced_line}」，"
                "原有通用任务往后顺延一天；会议/面试保持不变。"
            )
        result = apply_chat_actions(raw_actions)
        if result["failed"]:
            reply += f"\n（有 {len(result['failed'])} 个操作未执行，请核对原日程或补充信息。）"
        save_chat_turn(
            message or "（发送了一张截图）", reply, result["changes"]
        )
        return jsonify({"reply": reply, **result})

    @app.get("/uploads/<date_str>/<name>")
    @login_required
    def uploaded_file(date_str: str, name: str):
        if _valid_date(date_str) is None or not UPLOAD_NAME_RE.match(name):
            abort(404)
        directory = upload_dir / date_str
        path = directory / name
        if not path.is_file():
            abort(404)
        mime = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "gif": "image/gif",
            "webp": "image/webp",
        }[name.rsplit(".", 1)[1]]
        as_attachment = request.args.get("download") == "1"
        return send_from_directory(
            directory,
            name,
            mimetype=mime,
            as_attachment=as_attachment,
            download_name=f"截图-{date_str}{path.suffix}",
            max_age=3600,
        )

    @app.after_request
    def _security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; "
            "script-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        )
        if response.mimetype == "text/html":
            response.headers.setdefault("Cache-Control", "private, no-store")
        return response

    @app.errorhandler(404)
    def not_found(_error):
        if request.path.startswith(("/api/", "/uploads/")):
            return jsonify({"error": "未找到"}), 404
        return render_template("error.html", code=404, message="页面不存在"), 404

    @app.errorhandler(405)
    def method_not_allowed(_error):
        if request.path.startswith(("/api/", "/uploads/")):
            return jsonify({"error": "方法不允许"}), 405
        return render_template("error.html", code=405, message="方法不允许"), 405

    @app.errorhandler(413)
    def too_large(_error):
        if request.path.startswith("/api/"):
            if request.path.endswith(("/images", "/ai/parse")):
                message = "图片不能超过 10MB"
            else:
                message = "请求内容过大"
            return jsonify({"error": message}), 413
        return render_template("error.html", code=413, message="上传内容过大"), 413

    @app.errorhandler(500)
    def server_error(_error):
        if request.path.startswith("/api/"):
            return jsonify({"error": "服务器内部错误"}), 500
        return render_template("error.html", code=500, message="服务器出错了"), 500

    return app


if __name__ == "__main__":
    application = create_app()
    application.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        debug=os.environ.get("DEBUG", "") == "1",
    )
