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
import hmac
import os
import posixpath
import re
import secrets
import sqlite3
import time
import urllib.parse
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
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "") == "1",
        PERMANENT_SESSION_LIFETIME=dt.timedelta(days=30),
        APP_USERNAME=os.environ.get("APP_USERNAME", "admin"),
        APP_PASSWORD=os.environ.get("APP_PASSWORD", "admin123"),
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
                    filename   TEXT NOT NULL,
                    mime       TEXT NOT NULL,
                    size       INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_images_date ON images(date);
                """
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
        content_dates: set[str] = set()
        for row in db.execute(
            "SELECT date, event FROM days WHERE date BETWEEN ? AND ?",
            (start_iso, end_iso),
        ):
            if (row["event"] or "").strip():
                content_dates.add(row["date"])
        for row in db.execute(
            "SELECT DISTINCT date FROM images WHERE date BETWEEN ? AND ?",
            (start_iso, end_iso),
        ):
            content_dates.add(row["date"])

        grid_start = month_first - dt.timedelta(days=month_first.weekday())
        grid_end = month_last + dt.timedelta(days=6 - month_last.weekday())
        weeks = []
        cursor = grid_start
        while cursor <= grid_end:
            week = []
            for _ in range(7):
                week.append(
                    {
                        "iso": cursor.isoformat(),
                        "day": cursor.day,
                        "in_month": cursor.month == month_first.month,
                        "is_today": cursor == today,
                        "has_content": cursor.isoformat() in content_dates,
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
            "INSERT INTO images(date, filename, mime, size, created_at) VALUES (?, ?, ?, ?, ?)",
            (date_str, filename, mime, len(data), created_at),
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
            message = "图片不能超过 10MB" if request.path.endswith("/images") else "请求内容过大"
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
