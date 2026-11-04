"""核心功能测试：认证、事件、截图上传/查看/删除、月历标记。

运行: python -m unittest discover -s tests -v
"""

import base64
import shutil
import tempfile
import unittest
from pathlib import Path

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
        self.assertIn('/schedul/static/day.js', day.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
