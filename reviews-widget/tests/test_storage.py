import tempfile
import unittest
from pathlib import Path

from widget.demo import generate
from widget.models import Review, Site
from widget.storage import Storage


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.tmp.name) / "test.db")
        self.site = self.storage.add_site(Site.create("Кофейня"))

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def add(self, text, rating=5, status="published", author="Аня"):
        review = Review.build(self.site.key, text, rating, author)
        review.status = status
        return self.storage.add_review(review)

    # ------------------------------------------------------------- сайты

    def test_site_roundtrip(self):
        loaded = self.storage.get_site(self.site.key)
        self.assertEqual(loaded.name, "Кофейня")
        self.assertEqual(loaded.admin_token, self.site.admin_token)
        self.assertEqual(loaded.settings["theme"], "auto")

    def test_update_site_merges_settings(self):
        self.storage.update_site(self.site.key, settings={"theme": "dark"})
        updated = self.storage.get_site(self.site.key)
        self.assertEqual(updated.settings["theme"], "dark")
        # Остальные настройки не потерялись.
        self.assertEqual(updated.settings["limit"], 20)

    def test_update_site_ignores_unknown_columns(self):
        self.storage.update_site(self.site.key, admin_token="новый", key="взлом")
        self.assertEqual(self.storage.get_site(self.site.key).admin_token, "новый")

    def test_delete_site_removes_reviews(self):
        self.add("Отличный кофе и вежливые баристы")
        self.storage.delete_site(self.site.key)
        self.assertIsNone(self.storage.get_site(self.site.key))
        self.assertEqual(self.storage.counts(self.site.key)["total"], 0)

    def test_sites_with_bot(self):
        self.assertEqual(self.storage.sites_with_bot(), [])
        self.storage.update_site(self.site.key, telegram_token="123:AA")
        self.assertEqual([s.key for s in self.storage.sites_with_bot()], [self.site.key])

    # ------------------------------------------------------------ отзывы

    def test_duplicate_is_not_stored_twice(self):
        _, created_first = self.add("Отличный кофе и вежливые баристы")
        stored, created_again = self.add("Отличный кофе и вежливые баристы")
        self.assertTrue(created_first)
        self.assertFalse(created_again)
        self.assertEqual(self.storage.counts(self.site.key)["total"], 1)
        self.assertIsNotNone(stored.id)

    def test_publishing_sets_timestamp(self):
        review, _ = self.add("Отличный кофе и вежливые баристы", status="pending")
        self.assertEqual(review.published_at, "")
        published = self.storage.set_status(review.id, "published")
        self.assertTrue(published.published_at)

    def test_published_only_shows_published(self):
        self.add("Отличный кофе и вежливые баристы", status="published")
        self.add("Совсем не понравилось обслуживание", status="pending", author="Ира")
        self.add("Так себе, ждала заказ полчаса", status="rejected", author="Оля")
        self.assertEqual(len(self.storage.published(self.site.key)), 1)

    def test_pinned_comes_first(self):
        first, _ = self.add("Первый отзыв про отличный кофе", author="Аня")
        second, _ = self.add("Второй отзыв про уютный зал", author="Боря")
        self.storage.set_pinned(first.id, True)
        order = [review.id for review in self.storage.published(self.site.key)]
        self.assertEqual(order[0], first.id)
        self.assertIn(second.id, order)

    def test_min_rating_filter(self):
        self.add("Всё отлично, спасибо большое", 5, author="Аня")
        self.add("Ждал заказ очень долго, невесело", 2, author="Боря")
        self.assertEqual(len(self.storage.published(self.site.key, min_rating=3)), 1)
        self.assertEqual(len(self.storage.published(self.site.key)), 2)

    def test_limit_applied(self):
        for index in range(5):
            self.add(f"Отзыв номер {index} про отличный кофе", author=f"Гость{index}")
        self.assertEqual(len(self.storage.published(self.site.key, limit=3)), 3)

    def test_stats(self):
        self.add("Отличный кофе, всё понравилось", 5, author="Аня")
        self.add("Хороший зал и приятная музыка", 4, author="Боря")
        self.add("Ждал заказ очень долго, невесело", 2, author="Вика")
        self.add("Пока на модерации, в статистику не идёт", 1,
                 status="pending", author="Гриша")
        stats = self.storage.stats(self.site.key)
        self.assertEqual(stats["count"], 3)
        self.assertAlmostEqual(stats["average"], 3.67, places=2)
        self.assertEqual(stats["distribution"]["5"], 1)
        self.assertEqual(stats["distribution"]["3"], 0)

    def test_stats_empty_site(self):
        stats = self.storage.stats(self.site.key)
        self.assertEqual(stats, {"count": 0, "average": 0.0,
                                 "distribution": {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}})

    def test_counts_by_status(self):
        self.add("Опубликованный отзыв про кофе", author="Аня")
        self.add("Ждёт проверки этот отзыв", status="pending", author="Боря")
        counts = self.storage.counts(self.site.key)
        self.assertEqual((counts["published"], counts["pending"], counts["total"]), (1, 1, 2))

    def test_reviews_of_other_site_are_invisible(self):
        other = self.storage.add_site(Site.create("Другая компания"))
        self.add("Отзыв первой компании про кофе")
        self.assertEqual(self.storage.counts(other.key)["total"], 0)
        self.assertEqual(self.storage.published(other.key), [])

    def test_demo_data_lands_published(self):
        added = self.storage.add_many(generate(self.site.key, count=12, seed=7))
        self.assertGreater(added, 5)
        self.assertEqual(len(self.storage.published(self.site.key, limit=100)), added)
        self.assertGreater(self.storage.stats(self.site.key)["average"], 3)


if __name__ == "__main__":
    unittest.main()
