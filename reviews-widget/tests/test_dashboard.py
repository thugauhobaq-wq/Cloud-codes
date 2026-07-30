"""Импорт отзывов из базы reviews-dashboard.

Базу дашборда собираем здесь же руками: тест не должен зависеть от того,
что соседний проект установлен и что-то успел собрать.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from widget import dashboard
from widget.dashboard import DashboardError
from widget.models import Site
from widget.storage import Storage

# Схема повторяет reviews-dashboard/reviews/storage.py.
DASHBOARD_SCHEMA = """
CREATE TABLE reviews (
    uid         TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    company     TEXT NOT NULL,
    text        TEXT,
    pros        TEXT,
    cons        TEXT,
    reply       TEXT,
    rating      REAL,
    author      TEXT,
    date        TEXT,
    url         TEXT,
    external_id TEXT,
    sentiment   TEXT,
    score       REAL,
    keywords    TEXT
);
"""

ROWS = [
    # uid, source, company, text, pros, cons, reply, rating, author, date
    ("u1", "yandex", "Кофейня", "Отличный кофе и быстрый вайфай, сижу тут работать",
     "", "", "Спасибо, ждём снова!", 5.0, "Анна", "2026-06-01"),
    ("u2", "2gis", "Кофейня", "Приятное место, но по вечерам шумно и мало мест",
     "", "", "", 4.0, "Дмитрий", "2026-05-20"),
    ("u3", "wildberries", "Кофейня", "", "Быстро приехало, упаковка целая",
     "Ложка внутри погнута", "", 4.0, "Ольга", "2026-05-02"),
    ("u4", "ozon", "Кофейня", "Ждал две недели вместо трёх дней, больше не закажу",
     "", "", "", 2.0, "Сергей", "2026-04-11"),
    ("u5", "yandex", "Кофейня", "норм", "", "", "", 5.0, "Игорь", "2026-04-01"),
    ("u6", "yandex", "Кофейня", "Оценки нет, значит и звёзд не нарисуем никак",
     "", "", "", None, "Без оценки", "2026-03-15"),
    ("u7", "yandex", "Барбершоп", "Стрижка аккуратная, записали на удобное время",
     "", "", "", 5.0, "Пётр", "2026-06-10"),
]


class DashboardTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.source_db = Path(self.tmp.name) / "reviews.db"
        conn = sqlite3.connect(self.source_db)
        conn.executescript(DASHBOARD_SCHEMA)
        conn.executemany(
            """INSERT INTO reviews (uid, source, company, text, pros, cons, reply,
                                    rating, author, date)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ROWS,
        )
        conn.commit()
        conn.close()

        self.storage = Storage(Path(self.tmp.name) / "widget.db")
        self.site = self.storage.add_site(Site.create("Кофейня «Тёплый угол»"))

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def pull(self, **kwargs):
        return dashboard.pull(self.storage, self.site, self.source_db, **kwargs)

    # ------------------------------------------------------------ чтение

    def test_companies_summary(self):
        rows = dashboard.companies(self.source_db)
        by_name = {row["company"]: row for row in rows}
        self.assertEqual(by_name["Кофейня"]["total"], 6)
        self.assertEqual(by_name["Барбершоп"]["total"], 1)
        self.assertIn("Яндекс.Карты", by_name["Кофейня"]["titles"])
        self.assertIn("Ozon", by_name["Кофейня"]["titles"])
        # Отсортировано по количеству отзывов.
        self.assertEqual(rows[0]["company"], "Кофейня")

    def test_missing_db(self):
        with self.assertRaises(DashboardError):
            dashboard.companies(Path(self.tmp.name) / "нет.db")

    def test_foreign_db_rejected(self):
        alien = Path(self.tmp.name) / "alien.db"
        conn = sqlite3.connect(alien)
        conn.executescript("CREATE TABLE reviews (id INTEGER, что TEXT);")
        conn.commit()
        conn.close()
        with self.assertRaises(DashboardError) as ctx:
            dashboard.companies(alien)
        self.assertIn("reviews-dashboard", str(ctx.exception))

    def test_source_db_opened_read_only(self):
        conn = dashboard.connect(self.source_db)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM reviews")
        finally:
            conn.close()

    def test_rows_without_rating_are_not_read(self):
        rows = dashboard.read_rows(self.source_db, company="Кофейня")
        self.assertNotIn("u6", [row["uid"] for row in rows])
        self.assertEqual(len(rows), 5)

    def test_filters(self):
        self.assertEqual(
            len(dashboard.read_rows(self.source_db, company="Кофейня", source="yandex")), 2)
        self.assertEqual(
            len(dashboard.read_rows(self.source_db, company="Кофейня", min_rating=4)), 4)
        self.assertEqual(
            len(dashboard.read_rows(self.source_db, company="Кофейня", since="2026-05-01")), 3)
        self.assertEqual(len(dashboard.read_rows(self.source_db, limit=2)), 2)

    # ------------------------------------------------------------ маппинг

    def test_marketplace_pros_and_cons_become_text(self):
        row = [r for r in dashboard.read_rows(self.source_db) if r["uid"] == "u3"][0]
        review = dashboard.to_review(self.site.key, row)
        self.assertIn("Достоинства: Быстро приехало", review.text)
        self.assertIn("Недостатки: Ложка внутри погнута", review.text)

    def test_reply_and_meta_are_carried_over(self):
        row = [r for r in dashboard.read_rows(self.source_db) if r["uid"] == "u1"][0]
        review = dashboard.to_review(self.site.key, row)
        self.assertEqual(review.reply, "Спасибо, ждём снова!")
        self.assertEqual(review.source, "yandex")
        self.assertEqual(review.meta["dashboard_uid"], "u1")
        self.assertEqual(review.meta["company"], "Кофейня")
        self.assertEqual(review.created_at, "2026-06-01")
        self.assertEqual(review.rating, 5)

    # ------------------------------------------------------------- импорт

    def test_pull_lands_in_moderation(self):
        stats = self.pull(company="Кофейня")
        self.assertEqual(stats["seen"], 5)
        self.assertEqual(stats["added"], 4)      # «норм» слишком короткий
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(self.storage.counts(self.site.key)["pending"], 4)
        self.assertEqual(self.storage.published(self.site.key), [])

    def test_pull_with_publish(self):
        self.pull(company="Кофейня", publish=True)
        published = self.storage.published(self.site.key, limit=50)
        self.assertEqual(len(published), 4)
        # Порядок в виджете — по дате отзыва с площадки, а не по дате импорта.
        self.assertEqual(published[0].created_at, "2026-06-01")
        self.assertEqual(published[0].source, "yandex")

    def test_pull_min_rating_keeps_negatives_out(self):
        self.pull(company="Кофейня", min_rating=4, publish=True)
        ratings = [review.rating for review in self.storage.published(self.site.key)]
        self.assertNotIn(2, ratings)
        self.assertEqual(len(ratings), 3)

    def test_pull_single_source(self):
        self.pull(company="Кофейня", source="ozon")
        stored = self.storage.list_reviews(self.site.key)
        self.assertEqual([review.source for review in stored], ["ozon"])

    def test_repeated_pull_does_not_duplicate(self):
        first = self.pull(company="Кофейня")
        again = self.pull(company="Кофейня")
        self.assertEqual(again["added"], 0)
        self.assertEqual(again["duplicates"], first["added"])
        self.assertEqual(self.storage.counts(self.site.key)["total"], first["added"])

    def test_other_company_not_imported(self):
        self.pull(company="Кофейня")
        texts = " ".join(review.text for review in self.storage.list_reviews(self.site.key))
        self.assertNotIn("Стрижка", texts)

    def test_pull_all_companies(self):
        stats = self.pull()
        self.assertEqual(stats["added"], 5)      # 4 кофейни + 1 барбершоп

    def test_flagged_review_is_not_published_even_with_publish(self):
        conn = sqlite3.connect(self.source_db)
        conn.execute(
            """INSERT INTO reviews (uid, source, company, text, rating, author, date)
               VALUES ('u8', 'yandex', 'Кофейня',
                       'Хорошее место, подробности на www.example.ru про скидки', 5.0,
                       'Реклама', '2026-06-20')"""
        )
        conn.commit()
        conn.close()

        stats = self.pull(company="Кофейня", publish=True)
        self.assertEqual(stats["flagged"], 1)
        flagged = [r for r in self.storage.list_reviews(self.site.key) if r.author == "Реклама"]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0].status, "pending")
        self.assertIn("links", flagged[0].meta["flags"])


if __name__ == "__main__":
    unittest.main()
