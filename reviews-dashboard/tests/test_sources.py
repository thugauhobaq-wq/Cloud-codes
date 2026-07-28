"""Тесты парсеров: разбор ответов площадок на зафиксированных примерах."""

import json
import tempfile
import unittest
from pathlib import Path

from reviews.sources import get_source
from reviews.sources.gis2 import extract_branch_id, parse_payload_json
from reviews.sources.ozon import extract_product, parse_reviews
from reviews.sources.wildberries import extract_article, parse_feedbacks
from reviews.sources.yandex import extract_org_id, parse_html

YANDEX_HTML = """
<div class="business-reviews-card-view">
  <div class="business-review-view">
    <div class="business-review-view__author">
      <span class="business-review-view__author-name">Иван П.</span>
    </div>
    <meta itemprop="datePublished" content="2024-04-18">
    <div class="business-rating-badge-view__stars">
      <span class="business-rating-badge-view__star _full"></span>
      <span class="business-rating-badge-view__star _full"></span>
      <span class="business-rating-badge-view__star _full"></span>
      <span class="business-rating-badge-view__star _full"></span>
      <span class="business-rating-badge-view__star _empty"></span>
    </div>
    <div class="business-review-view__body">
      <span class="business-review-view__body-text">Хорошее место, вкусный кофе.</span>
    </div>
  </div>
  <div class="business-review-view">
    <span class="business-review-view__author-name">Аноним</span>
    <meta itemprop="datePublished" content="2024-05-02">
    <span class="business-review-view__body-text">Долго ждали заказ, официант нагрубил.</span>
  </div>
</div>
"""

YANDEX_JSONLD = """
<script type="application/ld+json">
{"@type": "LocalBusiness", "name": "Кафе",
 "review": [{"@type": "Review", "author": {"name": "Мария"},
             "datePublished": "2024-06-01", "reviewBody": "Очень уютно",
             "reviewRating": {"ratingValue": "5"}}]}
</script>
"""

GIS_JSON = {
    "meta": {"branch_rating": 4.3, "branch_reviews_count": 2},
    "reviews": [
        {"id": "a1", "text": "Отличный сервис", "rating": 5,
         "date_created": "2024-03-11T10:00:00+03:00", "user": {"name": "Пётр"},
         "official_answer": {"text": "Спасибо!"}},
        {"id": "a2", "text": "Грязно и дорого", "rating": 2,
         "date_created": "2024-03-15T10:00:00+03:00", "user": {"name": "Ольга"}},
    ],
}

WB_JSON = {
    "feedbacks": [
        {"id": "f1", "text": "Размер подошёл", "pros": "цена", "cons": "упаковка",
         "productValuation": 5, "createdDate": "2024-02-02T12:00:00Z",
         "wbUserDetails": {"name": "Анна"}, "answer": {"text": "Спасибо за отзыв"}},
        {"id": "f2", "text": "Пришёл брак", "productValuation": 1,
         "createdDate": "2024-02-05T12:00:00Z", "wbUserDetails": {"name": "Игорь"}},
    ]
}

OZON_JSON = {
    "widgetStates": {
        "webListReviews-123456-default-1": json.dumps({
            "reviews": [
                {"uuid": "o1", "score": 4, "createdAt": "2024-01-20T09:00:00Z",
                 "author": {"title": "Дмитрий"},
                 "content": {"comment": "Работает хорошо", "positive": "тихий",
                             "negative": "маркий"}},
            ]
        }),
        "someOtherWidget-1": "{}",
    }
}


class IdExtractionTest(unittest.TestCase):
    def test_yandex(self):
        self.assertEqual(
            extract_org_id("https://yandex.ru/maps/org/kafe/1234567890/reviews/"),
            "1234567890")
        self.assertEqual(extract_org_id("1234567890"), "1234567890")
        self.assertEqual(extract_org_id("кафе у дома"), "")

    def test_2gis(self):
        self.assertEqual(
            extract_branch_id("https://2gis.ru/moscow/firm/70000001006284412"),
            "70000001006284412")
        self.assertEqual(extract_branch_id("не ссылка"), "")

    def test_wildberries(self):
        self.assertEqual(
            extract_article("https://www.wildberries.ru/catalog/145766453/detail.aspx"),
            "145766453")
        self.assertEqual(extract_article("145766453"), "145766453")

    def test_ozon(self):
        self.assertEqual(
            extract_product("https://www.ozon.ru/product/chaynik-123456789/"),
            "chaynik-123456789")


class YandexParseTest(unittest.TestCase):
    def test_markup(self):
        items = parse_html(YANDEX_HTML)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["author"], "Иван П.")
        self.assertEqual(items[0]["rating"], 4.0)
        self.assertIn("вкусный кофе", items[0]["text"])
        self.assertEqual(items[1]["date"], "2024-05-02")

    def test_jsonld(self):
        items = parse_html(YANDEX_JSONLD)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["author"], "Мария")
        self.assertEqual(items[0]["rating"], 5.0)

    def test_payload_to_reviews(self):
        reviews = get_source("yandex").parse_payload(YANDEX_HTML, "Кафе")
        self.assertEqual(len(reviews), 2)
        self.assertEqual(reviews[0].source, "yandex")
        self.assertEqual(reviews[0].company, "Кафе")
        self.assertEqual(reviews[0].date, "2024-04-18")

    def test_broken_html_does_not_raise(self):
        self.assertEqual(parse_html("<div class='business-review-view'><span>"), [])


class TwoGisParseTest(unittest.TestCase):
    def test_parse(self):
        reviews = parse_payload_json(GIS_JSON, "Салон")
        self.assertEqual(len(reviews), 2)
        self.assertEqual(reviews[0].author, "Пётр")
        self.assertEqual(reviews[0].reply, "Спасибо!")
        self.assertEqual(reviews[0].date, "2024-03-11")
        self.assertEqual(reviews[1].rating, 2.0)

    def test_parse_from_string(self):
        reviews = parse_payload_json(json.dumps(GIS_JSON), "Салон")
        self.assertEqual(len(reviews), 2)

    def test_empty_payload(self):
        self.assertEqual(parse_payload_json({"reviews": []}, "X"), [])


class WildberriesParseTest(unittest.TestCase):
    def test_parse(self):
        reviews = parse_feedbacks(WB_JSON, "Чайник")
        self.assertEqual(len(reviews), 2)
        self.assertEqual(reviews[0].pros, "цена")
        self.assertEqual(reviews[0].cons, "упаковка")
        self.assertEqual(reviews[0].rating, 5.0)
        self.assertEqual(reviews[0].reply, "Спасибо за отзыв")
        self.assertEqual(reviews[1].date, "2024-02-05")


class OzonParseTest(unittest.TestCase):
    def test_parse_widget_states(self):
        reviews = parse_reviews(OZON_JSON, "Товар")
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0].author, "Дмитрий")
        self.assertEqual(reviews[0].pros, "тихий")
        self.assertEqual(reviews[0].rating, 4.0)


class FileSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_csv(self):
        path = self.dir / "export.csv"
        path.write_text("автор,текст,оценка,дата\nАнна,Всё отлично,5,2024-01-01\n",
                        encoding="utf-8")
        reviews = get_source("file").collect(str(path))
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0].author, "Анна")
        self.assertEqual(reviews[0].rating, 5.0)
        self.assertEqual(reviews[0].date, "2024-01-01")

    def test_json_list(self):
        path = self.dir / "export.json"
        path.write_text(json.dumps([
            {"author": "Ivan", "text": "Ужасно", "rating": 1, "date": "2024-02-02"},
        ]), encoding="utf-8")
        reviews = get_source("file").collect(str(path))
        self.assertEqual(reviews[0].text, "Ужасно")

    def test_json_wrapped(self):
        path = self.dir / "wrapped.json"
        path.write_text(json.dumps({"reviews": [{"text": "Норм", "rating": 3}]}),
                        encoding="utf-8")
        self.assertEqual(len(get_source("file").collect(str(path))), 1)


class DemoSourceTest(unittest.TestCase):
    def test_deterministic(self):
        first = get_source("demo").collect("Кафе", limit=25)
        second = get_source("demo").collect("Кафе", limit=25)
        self.assertEqual(len(first), 25)
        self.assertEqual([r.uid for r in first], [r.uid for r in second])

    def test_sources_are_mixed(self):
        reviews = get_source("demo").collect("Кафе", limit=120)
        self.assertGreaterEqual(len({review.source for review in reviews}), 3)

    def test_unknown_source(self):
        with self.assertRaises(KeyError):
            get_source("вконтакте")


if __name__ == "__main__":
    unittest.main()
