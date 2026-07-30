"""Сквозные проверки HTTP: поднимаем настоящий сервер на случайном порту."""

import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from widget.demo import generate
from widget.models import Review, Site
from widget.moderation import RateLimiter
from widget.server import WidgetHandler
from widget.storage import Storage


class ServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = str(Path(cls.tmp.name) / "server.db")
        WidgetHandler.db_path = cls.db
        WidgetHandler.public_url = ""
        WidgetHandler.log_message = lambda *args, **kwargs: None  # тихий лог в тестах
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), WidgetHandler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    def setUp(self):
        # Лимитер живёт в классе обработчика — между тестами его надо чистить.
        WidgetHandler.limiter = RateLimiter()
        self.storage = Storage(self.db)
        self.site = self.storage.add_site(Site.create("Кофейня", domains=["example.ru"]))

    def tearDown(self):
        self.storage.delete_site(self.site.key)
        self.storage.close()
        WidgetHandler.dashboard_db = ""

    def enable_dashboard(self):
        """Кладём рядом базу reviews-dashboard и включаем импорт с площадок."""
        path = Path(self.tmp.name) / "dashboard.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """CREATE TABLE IF NOT EXISTS reviews (
                   uid TEXT PRIMARY KEY, source TEXT, company TEXT, text TEXT,
                   pros TEXT, cons TEXT, reply TEXT, rating REAL, author TEXT, date TEXT);"""
        )
        conn.execute("DELETE FROM reviews")
        conn.executemany(
            "INSERT INTO reviews (uid, source, company, text, rating, author, date)"
            " VALUES (?,?,?,?,?,?,?)",
            [
                ("d1", "yandex", "Кофейня", "Отличный кофе и быстрый вайфай тут",
                 5.0, "Анна", "2026-06-01"),
                ("d2", "2gis", "Кофейня", "Приятное место, вечером бывает шумно",
                 4.0, "Дмитрий", "2026-05-20"),
            ],
        )
        conn.commit()
        conn.close()
        WidgetHandler.dashboard_db = str(path)
        return path

    # ------------------------------------------------------------ хелперы

    def fetch(self, path, data=None, headers=None, method=None):
        # Кириллица в пути — обычное дело для подобранных вручную адресов,
        # но в HTTP-строку запроса она должна уехать процентным кодированием.
        request = urllib.request.Request(
            self.base + urllib.parse.quote(path, safe="/?=&"),
            data=json.dumps(data).encode("utf-8") if data is not None else None,
            headers={"Content-Type": "application/json", **(headers or {})},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode("utf-8"), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8"), dict(error.headers)

    def json_fetch(self, path, **kwargs):
        status, body, headers = self.fetch(path, **kwargs)
        return status, json.loads(body or "{}"), headers

    def admin_headers(self):
        return {"X-Site": self.site.key, "X-Admin-Token": self.site.plain_token}

    def publish(self, text, rating=5, author="Аня"):
        review = Review.build(self.site.key, text, rating, author)
        review.status = "published"
        return self.storage.add_review(review)[0]

    # -------------------------------------------------------- отдача кода

    def test_widget_script_has_baked_config(self):
        status, body, headers = self.fetch(f"/w/{self.site.key}.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["Content-Type"])
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        self.assertIn("window.__REVIEWS_WIDGET__=", body)
        self.assertIn(self.site.key, body)
        self.assertIn("attachShadow", body)

    def test_unknown_site_script_is_harmless(self):
        status, body, headers = self.fetch("/w/s_нет.js")
        self.assertEqual(status, 404)
        self.assertIn("javascript", headers["Content-Type"])

    def test_generic_embed_js_served(self):
        status, body, _ = self.fetch("/embed.js")
        self.assertEqual(status, 200)
        self.assertIn("data-site", body)

    def test_asset_traversal_blocked(self):
        status, _, _ = self.fetch("/assets/../server.py")
        self.assertEqual(status, 404)

    # ------------------------------------------------------------- данные

    def test_widget_data_returns_only_published(self):
        self.publish("Отличный кофе и вежливые баристы")
        pending = Review.build(self.site.key, "Этот отзыв ещё не проверили", 5, "Боря")
        self.storage.add_review(pending)

        status, data, headers = self.json_fetch(f"/api/v1/widget?site={self.site.key}")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        self.assertEqual(len(data["reviews"]), 1)
        self.assertEqual(data["stats"]["count"], 1)
        self.assertEqual(data["settings"]["theme"], "auto")
        self.assertNotIn("contact", data["reviews"][0])

    def test_widget_data_respects_limit_and_min_rating(self):
        self.publish("Всё прекрасно, спасибо большое", 5, "Аня")
        self.publish("Ждал заказ очень долго, невесело", 2, "Боря")
        _, data, _ = self.json_fetch(f"/api/v1/widget?site={self.site.key}&min_rating=3")
        self.assertEqual(len(data["reviews"]), 1)
        _, data, _ = self.json_fetch(f"/api/v1/widget?site={self.site.key}&limit=1")
        self.assertEqual(len(data["reviews"]), 1)

    def test_widget_data_unknown_site(self):
        status, data, _ = self.json_fetch("/api/v1/widget?site=s_отсутствует")
        self.assertEqual(status, 404)
        self.assertIn("error", data)

    # -------------------------------------------------------- приём формы

    def submit(self, headers=None, **overrides):
        payload = {
            "site": self.site.key,
            "rating": 5,
            "text": "Заказывал дважды, всё привезли вовремя",
            "author": "Пётр",
            "elapsed": 30,
        }
        payload.update(overrides)
        return self.json_fetch("/api/v1/reviews", data=payload, headers=headers)

    def test_submit_lands_in_moderation(self):
        status, data, _ = self.submit()
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "pending")
        self.assertEqual(self.storage.counts(self.site.key)["pending"], 1)

    def test_submit_auto_publishes_when_enabled(self):
        self.storage.update_site(self.site.key, auto_approve=True, auto_approve_from=4)
        _, data, _ = self.submit(text="Прекрасное место, вернусь ещё не раз")
        self.assertEqual(data["status"], "published")

    def test_duplicate_submit_is_idempotent(self):
        self.submit()
        _, data, _ = self.submit()
        self.assertTrue(data["duplicate"])
        self.assertEqual(self.storage.counts(self.site.key)["total"], 1)

    def test_honeypot_rejected(self):
        status, data, _ = self.submit(website="http://bot.example")
        self.assertEqual(status, 400)
        self.assertEqual(self.storage.counts(self.site.key)["total"], 0)

    def test_spam_text_rejected(self):
        status, _, _ = self.submit(text="Заработок на крипто! Заходи http://casino.example")
        self.assertEqual(status, 400)

    def test_short_text_rejected(self):
        status, data, _ = self.submit(text="норм")
        self.assertEqual(status, 400)
        self.assertIn("короткий", data["error"])

    def test_bad_rating_rejected(self):
        self.assertEqual(self.submit(rating=9)[0], 400)

    def test_foreign_origin_rejected(self):
        status, data, _ = self.submit(headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)

    def test_allowed_origin_accepted_with_cors_header(self):
        status, _, headers = self.submit(headers={"Origin": "https://example.ru"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "https://example.ru")

    def test_rate_limit(self):
        for index in range(5):
            self.submit(text=f"Отзыв номер {index}, всё понравилось очень")
        status, data, _ = self.submit(text="Ещё один отзыв, шестой по счёту подряд")
        self.assertEqual(status, 429)

    def test_preflight(self):
        status, _, headers = self.fetch("/api/v1/reviews", method="OPTIONS",
                                        headers={"Origin": "https://example.ru"})
        self.assertEqual(status, 204)
        self.assertIn("POST", headers["Access-Control-Allow-Methods"])

    def test_xss_payload_is_neutralized(self):
        self.submit(text="<script>alert('xss')</script> Отличное обслуживание везде")
        stored = self.storage.list_reviews(self.site.key)[0]
        self.assertNotIn("<script>", stored.text)

    # ------------------------------------------------------------ админка

    def test_non_ascii_token_is_rejected_not_crashed(self):
        # Заголовки декодируются как latin-1, так что в токене может приехать
        # что угодно. Ответ должен быть 403, а не 500.
        status, _, _ = self.json_fetch("/api/admin/site", headers={
            "X-Site": self.site.key, "X-Admin-Token": "töken-é"})
        self.assertEqual(status, 403)

    def test_legacy_plaintext_token_works_and_is_upgraded(self):
        """База из первых версий: токен лежал открытым текстом."""
        # Настоящие токены всегда ASCII: их выдаёт secrets.token_urlsafe.
        plain = "legacy-token-from-old-database"
        self.storage.update_site(self.site.key, token_hash=plain)
        self.assertTrue(self.storage.get_site(self.site.key).token_needs_upgrade())

        status, _, _ = self.json_fetch("/api/admin/site", headers={
            "X-Site": self.site.key, "X-Admin-Token": plain})
        self.assertEqual(status, 200)

        upgraded = self.storage.get_site(self.site.key)
        self.assertTrue(upgraded.token_hash.startswith("sha256$"))
        self.assertNotIn(plain, upgraded.token_hash)
        # Тот же токен продолжает работать, уже против хеша.
        self.assertEqual(self.json_fetch("/api/admin/site", headers={
            "X-Site": self.site.key, "X-Admin-Token": plain})[0], 200)

    def test_admin_requires_token(self):
        self.assertEqual(self.json_fetch("/api/admin/site")[0], 403)
        self.assertEqual(
            self.json_fetch("/api/admin/site", headers={
                "X-Site": self.site.key, "X-Admin-Token": "wrong-token"})[0], 403)

    def test_admin_site_info(self):
        status, data, _ = self.json_fetch("/api/admin/site", headers=self.admin_headers())
        self.assertEqual(status, 200)
        self.assertEqual(data["site"]["key"], self.site.key)
        self.assertEqual(data["site"]["domains"], ["example.ru"])

    def test_admin_updates_settings(self):
        _, data, _ = self.json_fetch("/api/admin/site", headers=self.admin_headers(), data={
            "settings": {"theme": "dark", "limit": "5"},
            "domains": "example.ru, shop.example.ru",
            "auto_approve": True,
        })
        self.assertEqual(data["site"]["settings"]["theme"], "dark")
        self.assertEqual(data["site"]["settings"]["limit"], 5)
        self.assertEqual(data["site"]["domains"], ["example.ru", "shop.example.ru"])
        self.assertTrue(data["site"]["auto_approve"])

    def test_admin_publishes_review(self):
        self.submit()
        review_id = self.storage.list_reviews(self.site.key)[0].id
        status, data, _ = self.json_fetch(f"/api/admin/reviews/{review_id}",
                                          headers=self.admin_headers(),
                                          data={"action": "publish"})
        self.assertEqual(status, 200)
        self.assertEqual(data["review"]["status"], "published")
        _, widget, _ = self.json_fetch(f"/api/v1/widget?site={self.site.key}")
        self.assertEqual(len(widget["reviews"]), 1)

    def test_admin_reply_and_pin_and_delete(self):
        review = self.publish("Ждал заказ слишком долго, но вопрос решили", 3, "Боря")
        headers = self.admin_headers()
        _, data, _ = self.json_fetch(f"/api/admin/reviews/{review.id}", headers=headers,
                                     data={"action": "reply", "reply": "Извините, исправились!"})
        self.assertEqual(data["review"]["reply"], "Извините, исправились!")
        _, data, _ = self.json_fetch(f"/api/admin/reviews/{review.id}", headers=headers,
                                     data={"action": "pin"})
        self.assertTrue(data["review"]["pinned"])
        status, _, _ = self.json_fetch(f"/api/admin/reviews/{review.id}", headers=headers,
                                       data={"action": "delete"})
        self.assertEqual(status, 200)
        self.assertIsNone(self.storage.get_review(review.id))

    def test_admin_cannot_touch_other_sites_review(self):
        other = self.storage.add_site(Site.create("Чужая компания"))
        review = Review.build(other.key, "Отзыв чужой компании про сервис", 5)
        stored = self.storage.add_review(review)[0]
        status, _, _ = self.json_fetch(f"/api/admin/reviews/{stored.id}",
                                       headers=self.admin_headers(),
                                       data={"action": "publish"})
        self.assertEqual(status, 404)
        self.storage.delete_site(other.key)

    def test_admin_list_filters_by_status(self):
        self.submit()
        self.publish("Отличный кофе и вежливые баристы")
        _, data, _ = self.json_fetch("/api/admin/reviews?status=pending",
                                     headers=self.admin_headers())
        self.assertEqual(len(data["reviews"]), 1)
        self.assertEqual(data["counts"]["published"], 1)

    def test_admin_unknown_action(self):
        review = self.publish("Отличный кофе и вежливые баристы")
        status, _, _ = self.json_fetch(f"/api/admin/reviews/{review.id}",
                                       headers=self.admin_headers(),
                                       data={"action": "уничтожить"})
        self.assertEqual(status, 400)

    # -------------------------------------------- импорт с площадок (дашборд)

    def test_platforms_disabled_by_default(self):
        status, data, _ = self.json_fetch("/api/admin/platforms",
                                          headers=self.admin_headers())
        self.assertEqual(status, 200)
        self.assertFalse(data["available"])
        self.assertIn("--dashboard-db", data["hint"])
        self.assertEqual(data["companies"], [])

    def test_platforms_requires_token(self):
        self.enable_dashboard()
        self.assertEqual(self.json_fetch("/api/admin/platforms")[0], 403)
        self.assertEqual(
            self.json_fetch("/api/admin/platforms", data={"company": "Кофейня"})[0], 403)

    def test_platforms_lists_companies(self):
        self.enable_dashboard()
        _, data, _ = self.json_fetch("/api/admin/platforms", headers=self.admin_headers())
        self.assertTrue(data["available"])
        self.assertEqual(data["companies"][0]["company"], "Кофейня")
        self.assertEqual(data["companies"][0]["total"], 2)
        self.assertEqual(data["platforms"]["yandex"], "Яндекс.Карты")

    def test_platforms_pull_into_moderation(self):
        self.enable_dashboard()
        status, data, _ = self.json_fetch("/api/admin/platforms",
                                         headers=self.admin_headers(),
                                         data={"company": "Кофейня", "min_rating": 1})
        self.assertEqual(status, 200)
        self.assertEqual(data["stats"]["added"], 2)
        self.assertEqual(data["counts"]["pending"], 2)
        # На сайте их пока не видно — сначала модерация.
        _, widget, _ = self.json_fetch(f"/api/v1/widget?site={self.site.key}")
        self.assertEqual(widget["reviews"], [])

    def test_platforms_pull_with_publish_and_source_filter(self):
        self.enable_dashboard()
        _, data, _ = self.json_fetch("/api/admin/platforms", headers=self.admin_headers(),
                                     data={"company": "Кофейня", "source": "yandex",
                                           "publish": True})
        self.assertEqual(data["stats"]["added"], 1)
        _, widget, _ = self.json_fetch(f"/api/v1/widget?site={self.site.key}")
        self.assertEqual(len(widget["reviews"]), 1)
        self.assertEqual(widget["reviews"][0]["source"], "yandex")

    def test_platforms_pull_without_dashboard_is_rejected(self):
        status, data, _ = self.json_fetch("/api/admin/platforms",
                                         headers=self.admin_headers(),
                                         data={"company": "Кофейня"})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_platforms_broken_path_reported(self):
        WidgetHandler.dashboard_db = str(Path(self.tmp.name) / "нет-такой.db")
        _, data, _ = self.json_fetch("/api/admin/platforms", headers=self.admin_headers())
        self.assertFalse(data["available"])
        self.assertIn("не найдена", data["reason"])

    # ------------------------------------------------------------ страницы

    def test_form_page_renders_with_config(self):
        status, body, headers = self.fetch(f"/f/{self.site.key}")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(self.site.key, body)
        self.assertNotIn("__RW_FORM_CONFIG__", body)

    def test_preview_page(self):
        status, body, _ = self.fetch(f"/preview/{self.site.key}")
        self.assertEqual(status, 200)
        self.assertIn(f"/w/{self.site.key}.js", body)

    def test_landing_shows_first_site(self):
        status, body, _ = self.fetch("/")
        self.assertEqual(status, 200)
        self.assertIn("/w/", body)

    def test_admin_page_served(self):
        self.assertEqual(self.fetch("/admin")[0], 200)

    def test_health(self):
        status, data, _ = self.json_fetch("/healthz")
        self.assertTrue(data["ok"])

    def test_unknown_route(self):
        self.assertEqual(self.fetch("/чего-то-нет")[0], 404)

    def test_demo_data_visible_through_api(self):
        self.storage.add_many(generate(self.site.key, count=10, seed=3))
        _, data, _ = self.json_fetch(f"/api/v1/widget?site={self.site.key}")
        self.assertGreater(len(data["reviews"]), 3)
        self.assertTrue(data["reviews"][0]["initials"])


if __name__ == "__main__":
    unittest.main()
