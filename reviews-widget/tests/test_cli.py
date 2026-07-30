import io
import json
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

    def test_site_add_prints_embed_line(self):
        code, out, _ = self.run_cli("site", "add", "--name", "Кофейня")
        self.assertEqual(code, 0)
        with Storage(self.db) as storage:
            site = storage.list_sites()[0]
        self.assertIn(f"/w/{site.key}.js", out)
        self.assertIn(site.admin_token, out)

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

    def test_bot_without_token_explains(self):
        self.make_site()
        code, _, err = self.run_cli("bot")
        self.assertEqual(code, 1)
        self.assertIn("токен", err)


if __name__ == "__main__":
    unittest.main()
