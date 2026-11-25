"""核心功能测试：认证、事件、截图上传/查看/删除、月历标记。

运行: python -m unittest discover -s tests -v
"""

import base64
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.middleware.proxy_fix import ProxyFix

import app as app_module
from app import create_app

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
DATE = "2026-11-04"


class SchedulTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="schedul-test-"))
        self.app = create_app(self.tmp_dir)
        self.app.config.update(
            TESTING=True,
            APP_USERNAME="tester",
            APP_PASSWORD="secret",
        )
        self.client = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        app_module._FAILED_LOGINS.clear()
        app_module._AI_LAST_CALL.clear()

    # ---------- helpers ----------
    def login(self, client=None):
        client = client or self.client
        return client.post(
            "/login",
            data={"username": "tester", "password": "secret"},
            follow_redirects=False,
        )

    def login_ok(self):
        response = self.login()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")

    # ---------- 认证 ----------
    def test_requires_login(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

        api = self.client.get(f"/api/days/{DATE}")
        self.assertEqual(api.status_code, 401)
        self.assertEqual(api.get_json()["error"], "unauthorized")

    def test_login_page(self):
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn("账号", response.get_data(as_text=True))

    def test_login_wrong_password(self):
        response = self.client.post(
            "/login", data={"username": "tester", "password": "wrong"}
        )
        self.assertEqual(response.status_code, 401)
        self.assertIn("账号或密码不正确", response.get_data(as_text=True))

    def test_login_logout(self):
        self.login_ok()
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("月", page.get_data(as_text=True))

        logout = self.client.post("/logout")
        self.assertEqual(logout.status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_login_open_redirect_blocked(self):
        for next_value in (
            "//evil.example.com/",
            "/\\evil.example.com/x",
            "https://evil.example.com/x",
            "javascript:alert(1)",
            "/day/../..//evil.example.com",
        ):
            response = self.client.post(
                "/login",
                data={
                    "username": "tester",
                    "password": "secret",
                    "next": next_value,
                },
            )
            location = response.headers["Location"]
            self.assertTrue(
                location.startswith("/") and not location.startswith("//"),
                f"{next_value!r} -> {location!r}",
            )
            self.client.post("/logout")

    def test_login_throttle(self):
        for _ in range(10):
            self.client.post("/login", data={"username": "tester", "password": "x"})
        response = self.client.post(
            "/login", data={"username": "tester", "password": "secret"}
        )
        self.assertEqual(response.status_code, 429)

    # ---------- 月历 / 当天页 ----------
    def test_calendar_grid(self):
        self.login_ok()
        response = self.client.get("/?month=2026-11")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("2026年11月", html)
        # 2026-11-01 是周日，月历从 10-26 周一开始，共 6 周 42 格
        self.assertEqual(html.count('class="cell'), 42)
        self.assertIn("/day/2026-11-04", html)

    def test_day_page(self):
        self.login_ok()
        response = self.client.get(f"/day/{DATE}")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("11月4日", html)
        self.assertIn("星期三", html)
        self.assertIn("粘贴截图", html)
        # 事件直接可编辑（contenteditable）、资料标签与截图弹窗
        self.assertIn('id="event-editor"', html)
        self.assertIn('contenteditable="true"', html)
        self.assertIn('id="insert-data"', html)
        self.assertIn('id="data-modal"', html)
        self.assertIn('id="orphan-bar"', html)
        self.assertIn('id="shots"', html)
        self.assertNotIn('id="event-input"', html)
        self.assertIn("&lt;资料&gt;", html)

    def test_invalid_date(self):
        self.login_ok()
        self.assertEqual(self.client.get("/day/2026-13-01").status_code, 404)
        self.assertEqual(self.client.get("/api/days/2026-13-01").status_code, 404)
        self.assertEqual(self.client.get("/day/not-a-date").status_code, 404)

    # ---------- 事件 ----------
    def test_event_save_and_read(self):
        self.login_ok()
        payload = "09:00 站会\n14:00 评审"
        response = self.client.put(f"/api/days/{DATE}", json={"event": payload})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

        data = self.client.get(f"/api/days/{DATE}").get_json()
        self.assertEqual(data["event"], payload)

        day_html = self.client.get(f"/day/{DATE}").get_data(as_text=True)
        self.assertIn("站会", day_html)

        calendar = self.client.get("/?month=2026-11").get_data(as_text=True)
        self.assertIn("有日程", calendar)

    def test_event_update_overwrites(self):
        self.login_ok()
        self.client.put(f"/api/days/{DATE}", json={"event": "旧内容"})
        self.client.put(f"/api/days/{DATE}", json={"event": "新内容"})
        self.assertEqual(self.client.get(f"/api/days/{DATE}").get_json()["event"], "新内容")

    def test_event_cleared_removes_dot(self):
        self.login_ok()
        self.client.put(f"/api/days/{DATE}", json={"event": "临时"})
        self.client.put(f"/api/days/{DATE}", json={"event": ""})
        html = self.client.get("/?month=2026-11").get_data(as_text=True)
        self.assertNotIn("2026-11-04，有日程", html)

    def test_whitespace_only_event_has_no_dot(self):
        self.login_ok()
        for whitespace in ("   \n\t  ", "\u3000\u3000", "\r\n"):
            self.client.put(f"/api/days/{DATE}", json={"event": whitespace})
            html = self.client.get("/?month=2026-11").get_data(as_text=True)
            self.assertNotIn("2026-11-04，有日程", html, repr(whitespace))

    def test_event_kinds(self):
        self.assertEqual(
            app_module._event_kinds("15:00 公司：字节跳动｜岗位：AI 开发"),
            {"interview"},
        )
        self.assertEqual(
            app_module._event_kinds("19:30 事项：产品分享会｜地址：线上会议"),
            {"general"},
        )
        self.assertEqual(app_module._event_kinds("10:00 面试"), {"interview"})
        self.assertEqual(
            app_module._event_kinds(
                "10:00 事项：飞书视频会议：万志强 全栈｜地址：https://vc.feishu.cn/j/612518211"
            ),
            {"interview"},
        )
        self.assertEqual(
            app_module._event_kinds("19:30 事项：产品分享会｜地址：线上会议"),
            {"general"},
        )
        self.assertEqual(app_module._event_kinds("  \n\t "), set())

    def test_calendar_dots_distinguish_interview_and_general(self):
        self.login_ok()
        self.client.put(
            "/api/days/2026-11-20",
            json={"event": "15:00 公司：字节跳动｜岗位：AI 开发｜地址：腾讯会议"},
        )
        self.client.put(
            "/api/days/2026-11-21",
            json={"event": "19:30 事项：产品分享会｜地址：线上会议"},
        )
        self.client.put(
            "/api/days/2026-11-22",
            json={"event": "09:00 公司：甲｜岗位：乙\n19:00 事项：晚餐"},
        )
        html = self.client.get("/?month=2026-11").get_data(as_text=True)

        def cell(date: str) -> str:
            matched = re.search(
                rf'aria-label="{date}，有日程".*?</a>', html, re.S
            )
            self.assertIsNotNone(matched, date)
            return matched.group(0)

        interview_cell = cell("2026-11-20")
        self.assertIn("dot-interview", interview_cell)
        self.assertNotIn("dot-general", interview_cell)

        general_cell = cell("2026-11-21")
        self.assertIn("dot-general", general_cell)
        self.assertNotIn("dot-interview", general_cell)

        mixed_cell = cell("2026-11-22")
        self.assertIn("dot-interview", mixed_cell)
        self.assertIn("dot-general", mixed_cell)

    def test_calendar_repeats_dot_per_interview(self):
        self.login_ok()
        self.client.put(
            "/api/days/2026-11-25",
            json={
                "event": (
                    "10:00 事项：飞书视频会议：万志强 全栈｜地址：https://vc.feishu.cn/j/612518211\n"
                    "15:00 公司：万志强｜岗位：AI算法｜地址：会议号 838 921 676"
                )
            },
        )
        html = self.client.get("/?month=2026-11").get_data(as_text=True)
        matched = re.search(
            r'aria-label="2026-11-25，有日程".*?</a>', html, re.S
        )
        self.assertIsNotNone(matched)
        cell = matched.group(0)
        self.assertEqual(cell.count("dot-interview"), 2)
        self.assertNotIn("dot-general", cell)

    def test_api_405_is_json(self):
        self.login_ok()
        response = self.client.get(f"/api/days/{DATE}/images")
        self.assertEqual(response.status_code, 405)
        self.assertTrue(response.is_json)
        self.assertEqual(response.get_json()["error"], "方法不允许")

    def test_api_error_messages_are_utf8(self):
        self.login_ok()
        response = self.client.get("/api/days/2026-13-01")
        self.assertIn("无效日期", response.get_data(as_text=True))

    def test_event_validation(self):
        self.login_ok()
        self.assertEqual(self.client.put(f"/api/days/{DATE}", json={}).status_code, 400)
        self.assertEqual(
            self.client.put(f"/api/days/{DATE}", json={"event": 123}).status_code, 400
        )
        long_text = "字" * 20001
        self.assertEqual(
            self.client.put(f"/api/days/{DATE}", json={"event": long_text}).status_code,
            400,
        )

    def test_event_html_is_escaped(self):
        self.login_ok()
        self.client.put(f"/api/days/{DATE}", json={"event": "<script>alert(1)</script>"})
        html = self.client.get(f"/day/{DATE}").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)

    # ---------- 截图 ----------
    def test_image_upload_view_delete(self):
        self.login_ok()
        response = self.client.post(
            f"/api/days/{DATE}/images",
            data=PNG_1X1,
            headers={"Content-Type": "image/png"},
        )
        self.assertEqual(response.status_code, 201)
        image = response.get_json()["image"]
        self.assertEqual(image["mime"], "image/png")
        self.assertTrue(image["time_label"])
        self.assertEqual(image["size"], len(PNG_1X1))

        url = image["url"]
        served = self.client.get(url)
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.mimetype, "image/png")
        self.assertEqual(served.get_data(), PNG_1X1)
        served.close()

        download = self.client.get(url + "?download=1")
        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment", download.headers["Content-Disposition"])
        download.close()

        data = self.client.get(f"/api/days/{DATE}").get_json()
        self.assertEqual(len(data["images"]), 1)

        stored = list((self.tmp_dir / "uploads" / DATE).iterdir())
        self.assertEqual(len(stored), 1)

        removed = self.client.delete(f"/api/images/{image['id']}")
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(list((self.tmp_dir / "uploads" / DATE).iterdir()), [])

    def test_image_upload_rejects_svg_and_garbage(self):
        self.login_ok()
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        response = self.client.post(
            f"/api/days/{DATE}/images",
            data=svg,
            headers={"Content-Type": "image/svg+xml"},
        )
        self.assertEqual(response.status_code, 415)
        garbage = self.client.post(
            f"/api/days/{DATE}/images",
            data=b"not an image",
            headers={"Content-Type": "image/png"},
        )
        self.assertEqual(garbage.status_code, 415)
        empty = self.client.post(f"/api/days/{DATE}/images", data=b"")
        self.assertEqual(empty.status_code, 400)

    def test_image_belongs_to_slot(self):
        self.login_ok()
        first = self.client.post(
            f"/api/days/{DATE}/images?slot=0",
            data=PNG_1X1,
            headers={"Content-Type": "image/png"},
        )
        second = self.client.post(
            f"/api/days/{DATE}/images?slot=2",
            data=PNG_1X1,
            headers={"Content-Type": "image/png"},
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(first.get_json()["image"]["slot"], 0)
        self.assertEqual(second.get_json()["image"]["slot"], 2)

        images = self.client.get(f"/api/days/{DATE}").get_json()["images"]
        self.assertEqual([image["slot"] for image in images], [0, 2])

        bad = self.client.post(
            f"/api/days/{DATE}/images?slot=abc",
            data=PNG_1X1,
            headers={"Content-Type": "image/png"},
        )
        self.assertEqual(bad.status_code, 400)
        too_large = self.client.post(
            f"/api/days/{DATE}/images?slot=100",
            data=PNG_1X1,
            headers={"Content-Type": "image/png"},
        )
        self.assertEqual(too_large.status_code, 400)

    def test_image_upload_rejects_header_only_file(self):
        self.login_ok()
        signature_only = self.client.post(
            f"/api/days/{DATE}/images",
            data=b"\x89PNG\r\n\x1a\n",
            headers={"Content-Type": "image/png"},
        )
        self.assertEqual(signature_only.status_code, 415)
        truncated = self.client.post(
            f"/api/days/{DATE}/images",
            data=PNG_1X1[:20],
            headers={"Content-Type": "image/png"},
        )
        self.assertEqual(truncated.status_code, 415)

    def test_image_requires_login(self):
        self.assertEqual(
            self.client.post(
                f"/api/days/{DATE}/images",
                data=PNG_1X1,
                headers={"Content-Type": "image/png"},
            ).status_code,
            401,
        )
        self.assertEqual(self.client.get("/uploads/2026-11-04/f.png").status_code, 401)

    def test_upload_path_is_validated(self):
        self.login_ok()
        self.assertEqual(self.client.get("/uploads/2026-13-01/abc.png").status_code, 404)
        self.assertEqual(self.client.get("/uploads/2026-11-04/../../app.db").status_code, 404)
        self.assertEqual(self.client.get("/uploads/2026-11-04/evil.png").status_code, 404)
        self.assertEqual(self.client.delete("/api/images/99999").status_code, 404)

    def test_uploaded_image_marks_calendar(self):
        self.login_ok()
        self.client.post(
            f"/api/days/{DATE}/images",
            data=PNG_1X1,
            headers={"Content-Type": "image/png"},
        )
        html = self.client.get("/?month=2026-11").get_data(as_text=True)
        self.assertIn("2026-11-04，有日程", html)

    def test_headers(self):
        self.login_ok()
        response = self.client.get("/")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])

    def test_served_under_path_prefix(self):
        """部署在 /schedul/ 子路径下时，链接与重定向都要带前缀（模拟 Caddy + ProxyFix）。"""
        self.app.wsgi_app = ProxyFix(
            self.app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1
        )
        headers = {"X-Forwarded-Prefix": "/schedul"}
        anonymous = self.app.test_client()

        response = anonymous.get("/", headers=headers)
        self.assertEqual(response.status_code, 302)
        location = response.headers["Location"]
        self.assertTrue(location.startswith("/schedul/login"), location)
        self.assertIn("next=/schedul/", location)

        login = anonymous.post(
            "/login",
            data={"username": "tester", "password": "secret", "next": "/schedul/"},
            headers=headers,
        )
        self.assertEqual(login.status_code, 302)
        self.assertEqual(login.headers["Location"], "/schedul/")

        page = anonymous.get("/", headers=headers)
        html = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn('/schedul/static/style.css', html)
        self.assertIn('href="/schedul/day/2026-11-04"', html)

        day = anonymous.get("/day/2026-11-04", headers=headers)
        day_html = day.get_data(as_text=True)
        self.assertIn('/schedul/static/day.js', day_html)
        self.assertIn('data-api="/schedul/api/days/2026-11-04"', day_html)
        self.assertIn('data-image-api="/schedul/api/images/0"', day_html)


    def test_ai_normalization(self):
        payload = app_module._extract_json_object(
            '```json\n{"events":[{"kind":"interview","date":"2026-11-20","time":"9:05",'
            '"company":"字节跳动","role":"AI 应用开发","address":"腾讯会议 123"},'
            '{"kind":"general","date":"2026-11-21","time":"19:30",'
            '"title":"产品分享会","address":"线上会议 999"},'
            '{"date":"2026-11-22","time":"25:00","title":"提交材料"}]}\n```'
        )
        events = app_module._normalize_ai_events(payload)
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0]["kind"], "interview")
        self.assertEqual(events[0]["date"], "2026-11-20")
        self.assertEqual(events[0]["time"], "09:05")
        self.assertEqual(events[0]["company"], "字节跳动")
        self.assertEqual(events[0]["role"], "AI 应用开发")
        self.assertEqual(events[0]["address"], "腾讯会议 123")
        self.assertEqual(events[1]["kind"], "general")
        self.assertEqual(events[1]["time"], "19:30")
        self.assertEqual(events[1]["title"], "产品分享会")
        self.assertEqual(events[2]["kind"], "general")
        self.assertEqual(events[2]["time"], "")
        self.assertEqual(events[2]["title"], "提交材料")
        self.assertEqual(
            app_module._ai_event_line(events[0]),
            "09:05 公司：字节跳动｜岗位：AI 应用开发｜地址：腾讯会议 123",
        )
        self.assertEqual(
            app_module._ai_event_line(events[1]),
            "19:30 事项：产品分享会｜地址：线上会议 999",
        )
        self.assertEqual(app_module._ai_event_line(events[2]), "事项：提交材料")

    def test_ai_time_formats(self):
        cases = {
            "15:00": "15:00",
            "15:00:00": "15:00",
            "15:00-16:00": "15:00",
            "15：30": "15:30",
            "2026-11-17 15:00": "15:00",
            "下午3点": "15:00",
            "上午9点30分": "09:30",
            "晚上8点": "20:00",
            "3:00 PM": "15:00",
            "没有时间": "",
        }
        for raw, expected in cases.items():
            self.assertEqual(app_module._normalize_ai_time(raw), expected, raw)

    def test_ai_parse_requires_login(self):
        response = self.client.post(
            "/api/ai/parse", data=PNG_1X1, headers={"Content-Type": "image/png"}
        )
        self.assertEqual(response.status_code, 401)

    def test_ai_parse_without_config(self):
        self.login_ok()
        self.app.config["VISION_API_KEY"] = ""
        response = self.client.post(
            "/api/ai/parse", data=PNG_1X1, headers={"Content-Type": "image/png"}
        )
        self.assertEqual(response.status_code, 503)

    def test_ai_parse_adds_and_skips_events(self):
        self.login_ok()
        self.app.config.update(
            VISION_API_KEY="test-key",
            VISION_API_URL="https://example.invalid/v1/chat/completions",
            VISION_MODEL="test-model",
        )
        content = (
            '```json\n{"events":['
            '{"kind":"interview","date":"2026-11-20","time":"15:00","company":"字节跳动","role":"智能体开发","address":"腾讯会议 123"},'
            '{"kind":"general","date":"2026-11-20","time":"19:30","title":"产品分享会","address":"线上会议 999"},'
            '{"date":"2026-11-20","time":"10:00","company":"美团","role":"二面","address":""}'
            "]}\n```"
        )
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return body

        with patch.object(
            app_module.urllib.request, "urlopen", return_value=FakeResponse()
        ):
            response = self.client.post(
                "/api/ai/parse",
                data=PNG_1X1,
                headers={"Content-Type": "image/png"},
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(len(data["added"]), 3)
        self.assertEqual(data["skipped"], [])

        stored = self.client.get("/api/days/2026-11-20").get_json()["event"]
        self.assertIn("15:00 公司：字节跳动｜岗位：智能体开发｜地址：腾讯会议 123", stored)
        self.assertIn("19:30 事项：产品分享会｜地址：线上会议 999", stored)
        self.assertIn("10:00 公司：美团｜岗位：二面", stored)

        app_module._AI_LAST_CALL.clear()
        with patch.object(
            app_module.urllib.request, "urlopen", return_value=FakeResponse()
        ):
            second = self.client.post(
                "/api/ai/parse",
                data=PNG_1X1,
                headers={"Content-Type": "image/png"},
            )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json()["added"], [])
        self.assertEqual(len(second.get_json()["skipped"]), 3)

    def test_ai_chat_requires_login(self):
        response = self.client.post("/api/ai/chat", data={"message": "本周有安排吗"})
        self.assertEqual(response.status_code, 401)

    def test_ai_chat_adds_actions(self):
        self.login_ok()
        self.app.config.update(
            VISION_API_KEY="test-key",
            VISION_API_URL="https://example.invalid/v1/chat/completions",
            CHAT_MODEL="test-model",
        )
        content = json.dumps(
            {
                "reply": "已帮你排好下周三的面试。",
                "actions": [
                    {
                        "type": "create",
                        "date": "2026-11-25",
                        "time": "15:00",
                        "kind": "interview",
                        "company": "字节跳动",
                        "role": "AI 应用开发",
                        "address": "腾讯会议 123",
                    }
                ],
            },
            ensure_ascii=False,
        )
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return body

        app_module._AI_LAST_CALL.clear()
        with patch.object(
            app_module.urllib.request, "urlopen", return_value=FakeResponse()
        ):
            response = self.client.post(
                "/api/ai/chat",
                data={
                    "message": "帮我排下周三下午3点面试，字节跳动 AI 应用开发，腾讯会议 123",
                    "history": "[]",
                },
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["reply"], "已帮你排好下周三的面试。")
        self.assertEqual(len(data["added"]), 1)
        stored = self.client.get("/api/days/2026-11-25").get_json()["event"]
        self.assertIn("15:00 公司：字节跳动｜岗位：AI 应用开发｜地址：腾讯会议 123", stored)

    def test_ai_chat_query_only(self):
        self.login_ok()
        self.app.config.update(
            VISION_API_KEY="test-key",
            VISION_API_URL="https://example.invalid/v1/chat/completions",
            CHAT_MODEL="test-model",
        )
        content = json.dumps(
            {"reply": "那天下午没有安排。", "actions": []}, ensure_ascii=False
        )
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return body

        app_module._AI_LAST_CALL.clear()
        with patch.object(
            app_module.urllib.request, "urlopen", return_value=FakeResponse()
        ):
            response = self.client.post(
                "/api/ai/chat", data={"message": "2026-11-25 下午有空吗？"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["added"], [])
        self.assertEqual(self.client.get("/api/days/2026-11-25").get_json()["event"], "")

    def test_ai_chat_updates_and_shifts(self):
        self.login_ok()
        self.client.put(
            "/api/days/2026-11-25",
            json={
                "event": "css\n10:00 公司：某公司｜岗位：开发｜地址：腾讯会议"
            },
        )
        self.client.put("/api/days/2026-11-26", json={"event": "js"})
        self.client.put("/api/days/2026-11-27", json={"event": "vue"})
        self.app.config.update(
            VISION_API_KEY="test-key",
            VISION_API_URL="https://example.invalid/v1/chat/completions",
            CHAT_MODEL="test-model",
        )
        content = json.dumps(
            {
                "reply": "已把 25 日改成 html，其他学习任务往后推一天。",
                "actions": [
                    {
                        "type": "update",
                        "date": "2026-11-25",
                        "old_line": "css",
                        "new_date": "2026-11-25",
                        "new_line": "html",
                    },
                    {
                        "type": "shift",
                        "after_date": "2026-11-25",
                        "days": 1,
                        "scope": "general",
                    },
                ],
            },
            ensure_ascii=False,
        )
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return body

        app_module._AI_LAST_CALL.clear()
        with patch.object(
            app_module.urllib.request, "urlopen", return_value=FakeResponse()
        ):
            response = self.client.post(
                "/api/ai/chat",
                data={"message": "把25日改成html"},
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(len(data["changes"]), 2)
        self.assertEqual(data["failed"], [])
        self.assertEqual(
            self.client.get("/api/days/2026-11-25").get_json()["event"],
            "10:00 公司：某公司｜岗位：开发｜地址：腾讯会议\nhtml",
        )
        self.assertEqual(self.client.get("/api/days/2026-11-26").get_json()["event"], "")
        self.assertEqual(self.client.get("/api/days/2026-11-27").get_json()["event"], "js")
        self.assertEqual(self.client.get("/api/days/2026-11-28").get_json()["event"], "vue")
        self.assertEqual(data["dots"]["2026-11-27"]["general"], 1)

    def test_ai_chat_delete_line(self):
        self.login_ok()
        self.client.put(
            "/api/days/2026-11-25", json={"event": "css\njs"}
        )
        self.app.config.update(
            VISION_API_KEY="test-key",
            VISION_API_URL="https://example.invalid/v1/chat/completions",
            CHAT_MODEL="test-model",
        )
        content = json.dumps(
            {
                "reply": "已删除。",
                "actions": [
                    {"type": "delete", "date": "2026-11-25", "line": "css"}
                ],
            },
            ensure_ascii=False,
        )
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return body

        app_module._AI_LAST_CALL.clear()
        with patch.object(
            app_module.urllib.request, "urlopen", return_value=FakeResponse()
        ):
            response = self.client.post(
                "/api/ai/chat", data={"message": "删掉25日的css"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.get("/api/days/2026-11-25").get_json()["event"], "js"
        )

    def test_ai_chat_insert_and_shift(self):
        self.login_ok()
        self.client.put(
            "/api/days/2026-11-25",
            json={
                "event": "css\n10:00 公司：某公司｜岗位：开发｜地址：腾讯会议"
            },
        )
        self.client.put("/api/days/2026-11-26", json={"event": "js"})
        self.client.put("/api/days/2026-11-27", json={"event": "vue"})
        self.app.config.update(
            VISION_API_KEY="test-key",
            VISION_API_URL="https://example.invalid/v1/chat/completions",
            CHAT_MODEL="test-model",
        )
        content = json.dumps(
            {
                "reply": "模型只做了后移，没有插入。",
                "actions": [
                    {
                        "type": "update",
                        "date": "2026-11-25",
                        "old_line": "css",
                        "new_date": "2026-11-25",
                        "new_line": "html",
                    },
                    {
                        "type": "shift",
                        "after_date": "2026-11-25",
                        "days": 1,
                        "scope": "general",
                    },
                ],
            },
            ensure_ascii=False,
        )
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return body

        app_module._AI_LAST_CALL.clear()
        with patch.object(
            app_module.urllib.request, "urlopen", return_value=FakeResponse()
        ):
            response = self.client.post(
                "/api/ai/chat",
                data={"message": "帮我在25日改成html，其他往后推"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.get("/api/days/2026-11-25").get_json()["event"],
            "10:00 公司：某公司｜岗位：开发｜地址：腾讯会议\nhtml",
        )
        self.assertEqual(self.client.get("/api/days/2026-11-26").get_json()["event"], "css")
        self.assertEqual(self.client.get("/api/days/2026-11-27").get_json()["event"], "js")
        self.assertEqual(self.client.get("/api/days/2026-11-28").get_json()["event"], "vue")


    def test_ai_chat_history_persisted(self):
        self.login_ok()
        self.app.config.update(
            VISION_API_KEY="test-key",
            VISION_API_URL="https://example.invalid/v1/chat/completions",
            CHAT_MODEL="test-model",
        )
        content = json.dumps(
            {"reply": "你好，我在。", "actions": []}, ensure_ascii=False
        )
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return body

        app_module._AI_LAST_CALL.clear()
        with patch.object(
            app_module.urllib.request, "urlopen", return_value=FakeResponse()
        ):
            response = self.client.post(
                "/api/ai/chat", data={"message": "你好"}
            )
        self.assertEqual(response.status_code, 200)

        history = self.client.get("/api/ai/chat/history").get_json()["messages"]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["role"], "user")
        self.assertEqual(history[0]["content"], "你好")
        self.assertEqual(history[1]["role"], "assistant")
        self.assertEqual(history[1]["content"], "你好，我在。")

    def test_ai_chat_history_requires_login(self):
        response = self.client.get("/api/ai/chat/history")
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
