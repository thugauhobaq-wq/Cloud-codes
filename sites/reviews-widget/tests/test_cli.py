import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from widget.cli import main
from widget.storage import Storage


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "cli.db")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--db", self.db, *args])
        return code, out.getvalue(), err.getvalue()

    def make_site(self, *extra):
        self.run_cli("site", "add", "--name", "Кофейня", *extra)
        with Storage(self.db) as storage:
            return storage.list_sites()[0]

    # ------------------------------------------------------------- сайты

    def test_embed_line_uses_public_url_from_env(self):
        """В контейнере адрес задан env — подсказки не должны врать про localhost."""
        site = self.make_site()
        _, out, _ = self.run_cli("site", "show", "--site", site.key)
        self.assertIn("http://localhost:8080/w/", out)

        os.environ["WIDGET_PUBLIC_URL"] = "https://widgets.example.ru/"
        try:
            _, out, _ = self.run_cli("site", "show", "--site", site.key)
        finally:
            del os.environ["WIDGET_PUBLIC_URL"]
        self.assertIn(f'https://widgets.example.ru/w/{site.key}.js', out)
        self.assertIn(f"https://widgets.example.ru/f/{site.key}", out)
        self.assertNotIn("localhost", out)

    def test_site_show_does_not_print_token(self):
        site = self.make_site()
        _, out, _ = self.run_cli("site", "show", "--site", site.key)
        self.assertIn("нельзя", out)
        self.assertIn("rotate-token", out)

    def test_rotate_token_gives_new_working_token(self):
        site = self.make_site()
        old_hash = site.token_hash
        _, out, _ = self.run_cli("site", "rotate-token", "--site", site.key)
        self.assertIn("Новый токен", out)
        token = [line.strip() for line in out.splitlines() if line.startswith("  ")][0]

        with Storage(self.db) as storage:
            updated = storage.get_site(site.key)
        self.assertNotEqual(updated.token_hash, old_hash)
        self.assertTrue(updated.check_token(token))

    def test_site_add_prints_embed_line(self):
        code, out, _ = self.run_cli("site", "add", "--name", "Кофейня")
        self.assertEqual(code, 0)
        with Storage(self.db) as storage:
            site = storage.list_sites()[0]
        self.assertIn(f"/w/{site.key}.js", out)
        # Токен печатается один раз при создании и в базе лежит хешем.
        self.assertRegex(out, r"токен:\s+\S{20,}")
        self.assertTrue(site.token_hash.startswith("sha256$"))

    def test_site_add_with_demo_fills_reviews(self):
        site = self.make_site("--demo")
        with Storage(self.db) as storage:
            self.assertGreater(storage.counts(site.key)["published"], 5)

    def test_site_list_empty(self):
        _, out, _ = self.run_cli("site", "list")
        self.assertIn("Пока ни одного сайта", out)

    def test_site_set_updates_settings_and_domains(self):
        site = self.make_site()
        self.run_cli("site", "set", "--site", site.key,
                     "--domains", "example.ru,www.example.ru",
                     "--auto-approve", "on",
                     "--setting", "theme=dark", "--setting", "limit=7")
        with Storage(self.db) as storage:
            updated = storage.get_site(site.key)
        self.assertEqual(updated.domains, ["example.ru", "www.example.ru"])
        self.assertTrue(updated.auto_approve)
        self.assertEqual(updated.settings["theme"], "dark")
        self.assertEqual(updated.settings["limit"], 7)

    def test_site_set_ignores_broken_setting(self):
        site = self.make_site()
        code, _, err = self.run_cli("site", "set", "--site", site.key, "--setting", "мусор")
        self.assertEqual(code, 0)
        self.assertIn("без знака", err)

    def test_unknown_site_returns_error_code(self):
        code, _, err = self.run_cli("site", "show", "--site", "s_нет")
        self.assertEqual(code, 1)
        self.assertIn("не найден", err)

    def test_site_rm(self):
        site = self.make_site("--demo")
        self.run_cli("site", "rm", "--site", site.key)
        with Storage(self.db) as storage:
            self.assertEqual(storage.list_sites(), [])

    # ------------------------------------------------------------ отзывы

    def test_demo_and_reviews_listing(self):
        site = self.make_site()
        self.run_cli("demo", "--site", site.key, "--count", "8", "--seed", "1")
        _, out, _ = self.run_cli("reviews", "--site", site.key, "--status", "published")
        self.assertIn("★", out)

    def test_moderate_publishes(self):
        site = self.make_site()
        path = Path(self.tmp.name) / "reviews.json"
        path.write_text(json.dumps([
            {"text": "Хороший сервис, всем рекомендую", "rating": 5, "author": "Аня"},
        ]), encoding="utf-8")
        self.run_cli("import", "--site", site.key, "--file", str(path))

        with Storage(self.db) as storage:
            review = storage.list_reviews(site.key)[0]
            self.assertEqual(review.status, "pending")
        self.run_cli("moderate", "--id", str(review.id), "--action", "publish")
        with Storage(self.db) as storage:
            self.assertEqual(storage.get_review(review.id).status, "published")

    def test_import_json_and_csv(self):
        site = self.make_site()
        json_path = Path(self.tmp.name) / "reviews.json"
        json_path.write_text(json.dumps([
            {"text": "Хороший сервис, всем рекомендую", "rating": 5, "author": "Аня"},
            {"text": "коротко", "rating": 5},
        ], ensure_ascii=False), encoding="utf-8")
        _, out, _ = self.run_cli("import", "--site", site.key, "--file", str(json_path),
                                 "--publish")
        self.assertIn("Загружено: 1", out)
        self.assertIn("пропущено: 1", out)

        csv_path = Path(self.tmp.name) / "reviews.csv"
        csv_path.write_text(
            "Имя;Оценка;Отзыв\nБорис;4;Всё понравилось, приеду ещё\n", encoding="utf-8")
        _, out, _ = self.run_cli("import", "--site", site.key, "--file", str(csv_path))
        self.assertIn("Загружено: 1", out)

        with Storage(self.db) as storage:
            texts = [review.text for review in storage.list_reviews(site.key)]
        self.assertIn("Всё понравилось, приеду ещё", texts)

    def test_import_missing_file(self):
        site = self.make_site()
        code, _, err = self.run_cli("import", "--site", site.key, "--file", "/нет/файла.json")
        self.assertEqual(code, 1)
        self.assertIn("не найден", err)

    def test_export_is_valid_json(self):
        site = self.make_site("--demo")
        _, out, _ = self.run_cli("export", "--site", site.key)
        data = json.loads(out)
        self.assertGreater(len(data), 5)
        self.assertIn("status", data[0])

    # -------------------------------------------- импорт с площадок (дашборд)

    def make_dashboard_db(self):
        path = Path(self.tmp.name) / "dashboard.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """CREATE TABLE reviews (
                   uid TEXT PRIMARY KEY, source TEXT, company TEXT, text TEXT,
                   pros TEXT, cons TEXT, reply TEXT, rating REAL, author TEXT, date TEXT);"""
        )
        conn.executemany(
            "INSERT INTO reviews (uid, source, company, text, rating, author, date)"
            " VALUES (?,?,?,?,?,?,?)",
            [
                ("d1", "yandex", "Кофейня", "Отличный кофе и быстрый вайфай тут",
                 5.0, "Анна", "2026-06-01"),
                ("d2", "ozon", "Кофейня", "Ждал две недели вместо трёх дней подряд",
                 2.0, "Сергей", "2026-05-02"),
            ],
        )
        conn.commit()
        conn.close()
        return path

    def test_pull_list_shows_companies(self):
        site = self.make_site()
        source = self.make_dashboard_db()
        code, out, _ = self.run_cli("pull", "--site", site.key, "--from", str(source), "--list")
        self.assertEqual(code, 0)
        self.assertIn("Кофейня", out)
        self.assertIn("Яндекс.Карты", out)

    def test_pull_imports_into_moderation(self):
        site = self.make_site()
        source = self.make_dashboard_db()
        code, out, _ = self.run_cli("pull", "--site", site.key, "--from", str(source))
        self.assertEqual(code, 0)
        self.assertIn("Добавлено: 2", out)
        with Storage(self.db) as storage:
            self.assertEqual(storage.counts(site.key)["pending"], 2)

    def test_pull_min_rating_and_publish(self):
        site = self.make_site()
        source = self.make_dashboard_db()
        self.run_cli("pull", "--site", site.key, "--from", str(source),
                     "--min-rating", "4", "--publish")
        with Storage(self.db) as storage:
            published = storage.published(site.key)
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0].source, "yandex")

    def test_pull_missing_dashboard_db(self):
        site = self.make_site()
        code, _, err = self.run_cli("pull", "--site", site.key, "--from", "/нет/reviews.db")
        self.assertEqual(code, 1)
        self.assertIn("не найдена", err)

    def test_bot_without_token_explains(self):
        self.make_site()
        code, _, err = self.run_cli("bot")
        self.assertEqual(code, 1)
        self.assertIn("токен", err)


if __name__ == "__main__":
    unittest.main()
