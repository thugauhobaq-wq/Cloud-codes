import tempfile
import unittest
from pathlib import Path

from reviews.models import Review, parse_date
from reviews.storage import Storage


class ParseDateTest(unittest.TestCase):
    def test_iso(self):
        self.assertEqual(parse_date("2024-03-12"), "2024-03-12")

    def test_iso_with_time(self):
        self.assertEqual(parse_date("2024-03-12T18:44:01Z"), "2024-03-12")

    def test_russian_format(self):
        self.assertEqual(parse_date("12.03.2024"), "2024-03-12")

    def test_russian_words(self):
        self.assertEqual(parse_date("12 марта 2024"), "2024-03-12")

    def test_unix_seconds_and_millis(self):
        self.assertEqual(parse_date(1710201600), "2024-03-12")
        self.assertEqual(parse_date(1710201600000), "2024-03-12")

    def test_garbage_is_empty(self):
        self.assertEqual(parse_date("вчера вечером"), "")
        self.assertEqual(parse_date(None), "")


class ReviewTest(unittest.TestCase):
    def test_html_is_stripped(self):
        review = Review(source="demo", company="X", text="<b>Отлично</b>&nbsp;совсем")
        self.assertEqual(review.text, "Отлично совсем")

    def test_rating_is_clamped(self):
        self.assertEqual(Review(source="demo", company="X", rating=9).rating, 5.0)
        self.assertEqual(Review(source="demo", company="X", rating=0).rating, 1.0)
        self.assertIsNone(Review(source="demo", company="X", rating="ой").rating)

    def test_uid_is_stable_and_unique(self):
        first = Review(source="demo", company="X", text="Текст", external_id="1")
        same = Review(source="demo", company="X", text="Текст", external_id="1")
        other = Review(source="demo", company="X", text="Текст", external_id="2")
        self.assertEqual(first.uid, same.uid)
        self.assertNotEqual(first.uid, other.uid)

    def test_full_text_includes_pros_and_cons(self):
        review = Review(source="wildberries", company="X", text="Норм",
                        pros="цена", cons="упаковка")
        self.assertIn("Достоинства: цена", review.full_text)
        self.assertIn("Недостатки: упаковка", review.full_text)

    def test_empty_author_becomes_anonymous(self):
        self.assertEqual(Review(source="demo", company="X").author, "Аноним")


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def _review(self, **kwargs):
        data = {"source": "demo", "company": "Кафе", "text": "Отлично",
                "rating": 5.0, "date": "2024-05-01", "sentiment": "positive",
                "score": 0.8, "external_id": "1"}
        data.update(kwargs)
        return Review(**data)

    def test_save_and_count(self):
        self.assertEqual(self.storage.save([self._review()]), 1)
        self.assertEqual(self.storage.count(), 1)

    def test_duplicates_are_ignored(self):
        self.storage.save([self._review()])
        added = self.storage.save([self._review()])
        self.assertEqual(added, 0)
        self.assertEqual(self.storage.count(), 1)

    def test_filters(self):
        self.storage.save([
            self._review(external_id="1", sentiment="positive"),
            self._review(external_id="2", sentiment="negative", text="Ужасно",
                         source="yandex", date="2024-06-01"),
        ])
        self.assertEqual(len(self.storage.fetch(sentiment="negative")), 1)
        self.assertEqual(len(self.storage.fetch(source="yandex")), 1)
        self.assertEqual(len(self.storage.fetch(query="Ужас")), 1)
        self.assertEqual(len(self.storage.fetch(date_from="2024-05-15")), 1)
        self.assertEqual(len(self.storage.fetch(company="Кафе")), 2)

    def test_pagination(self):
        self.storage.save([self._review(external_id=str(i)) for i in range(10)])
        page = self.storage.fetch(limit=4, offset=4)
        self.assertEqual(len(page), 4)
        self.assertEqual(self.storage.total_matching(company="Кафе"), 10)

    def test_companies_summary(self):
        self.storage.save([self._review(), self._review(external_id="2", company="Другая")])
        companies = {item["company"]: item for item in self.storage.companies()}
        self.assertIn("Кафе", companies)
        self.assertEqual(companies["Кафе"]["avg_rating"], 5.0)

    def test_keywords_roundtrip(self):
        review = self._review()
        review.keywords = ["вкусно", "быстро"]
        self.storage.save([review])
        self.assertEqual(self.storage.fetch()[0]["keywords"], ["вкусно", "быстро"])


if __name__ == "__main__":
    unittest.main()
