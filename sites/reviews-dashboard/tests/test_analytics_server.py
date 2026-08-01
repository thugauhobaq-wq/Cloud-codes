import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from reviews.analytics import summarize
from reviews.pipeline import collect, enrich
from reviews.models import Review
from reviews.server import DashboardHandler
from reviews.storage import Storage


def rows(reviews):
    return [review.to_dict() for review in enrich(reviews)]


class SummarizeTest(unittest.TestCase):
    def setUp(self):
        self.rows = rows([
            Review(source="yandex", company="X", text="Отличное место, всем рекомендую",
                   rating=5, date="2024-05-01"),
            Review(source="yandex", company="X", text="Ужасно, персонал нахамил",
                   rating=1, date="2024-05-20", reply="Извините"),
            Review(source="2gis", company="X", text="Обычное место", rating=3,
                   date="2024-06-02"),
            Review(source="2gis", company="X", text="Доставка опоздала, курьер нагрубил",
                   rating=2, date="2024-06-14"),
        ])

    def test_empty_input(self):
        data = summarize([])
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["sentiment"]["positive"], 0)
        self.assertEqual(data["sources"], [])

    def test_totals(self):
        data = summarize(self.rows)
        self.assertEqual(data["total"], 4)
        self.assertEqual(data["avg_rating"], 2.75)
        self.assertEqual(data["with_reply"], 1)
        self.assertAlmostEqual(data["reply_rate"], 0.25)

    def test_sentiment_split(self):
        data = summarize(self.rows)
        self.assertEqual(data["sentiment"]["positive"], 1)
        self.assertGreaterEqual(data["sentiment"]["negative"], 2)
        self.assertAlmostEqual(sum(data["sentiment_share"].values()), 1.0, places=2)

    def test_sources_breakdown(self):
        data = summarize(self.rows)
        by_name = {item["source"]: item for item in data["sources"]}
        self.assertEqual(by_name["yandex"]["total"], 2)
        self.assertEqual(by_name["2gis"]["total"], 2)

    def test_timeline_is_sorted_by_month(self):
        data = summarize(self.rows)
        months = [point["month"] for point in data["timeline"]]
        self.assertEqual(months, sorted(months))
        self.assertEqual(months, ["2024-05", "2024-06"])

    def test_ratings_histogram(self):
        data = summarize(self.rows)
        self.assertEqual(data["ratings"]["5"], 1)
        self.assertEqual(data["ratings"]["1"], 1)

    def test_highlights(self):
        data = summarize(self.rows)
        self.assertIn("рекомендую", data["highlights"]["best"]["text"])
        self.assertEqual(data["highlights"]["latest"]["date"], "2024-06-14")

    def test_aspects_and_keywords(self):
        data = summarize(self.rows)
        aspects = {item["aspect"] for item in data["aspects"]}
        self.assertIn("Персонал", aspects)
        self.assertTrue(data["keywords"]["negative"])

    def test_period(self):
        data = summarize(self.rows)
        self.assertEqual(data["period"], {"from": "2024-05-01", "to": "2024-06-14"})

    def test_trend_status_present(self):
        data = summarize(self.rows)
        self.assertIn("status", data["trend"])


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.tmp.name) / "p.db")

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def test_collect_demo_writes_to_storage(self):
        stats = collect("demo", "Кафе", limit=30, storage=self.storage)
        self.assertEqual(stats["fetched"], 30)
        self.assertEqual(stats["added"], 30)
        self.assertEqual(self.storage.count(), 30)

    def test_second_run_finds_duplicates(self):
        collect("demo", "Кафе", limit=20, storage=self.storage)
        stats = collect("demo", "Кафе", limit=20, storage=self.storage)
        self.assertEqual(stats["added"], 0)
        self.assertEqual(stats["duplicates"], 20)

    def test_enrich_sets_sentiment(self):
        reviews = enrich([Review(source="demo", company="X", text="Всё отлично", rating=5)])
        self.assertEqual(reviews[0].sentiment, "positive")
        self.assertTrue(reviews[0].keywords)

    def test_collect_from_file(self):
        path = Path(self.tmp.name) / "export.json"
        path.write_text(json.dumps([{"text": "Ужасно", "rating": 1, "date": "2024-01-01"}]),
                        encoding="utf-8")
        stats = collect("file", str(path), company="Импорт", storage=self.storage,
                        from_file=str(path))
        self.assertEqual(stats["added"], 1)
        self.assertEqual(self.storage.fetch()[0]["sentiment"], "negative")


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db_path = str(Path(cls.tmp.name) / "srv.db")
        with Storage(db_path) as storage:
            collect("demo", "Тест-компания", limit=40, storage=storage)

        handler = type("Bound", (DashboardHandler,), {"db_path": db_path})
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as response:
            return response.status, json.loads(response.read().decode())

    def test_summary_endpoint(self):
        # Фильтруем по компании: тест сбора в этом же классе добавляет свои отзывы.
        status, data = self.get("/api/summary?company=%D0%A2%D0%B5%D1%81%D1%82-%D0%BA%D0%BE%D0%BC%D0%BF%D0%B0%D0%BD%D0%B8%D1%8F")
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 40)
        self.assertIn("timeline", data)
        self.assertTrue(data["companies"])

    def test_summary_filters(self):
        _, data = self.get("/api/summary?sentiment=negative")
        self.assertTrue(all(item >= 0 for item in data["sentiment"].values()))
        self.assertEqual(data["sentiment"]["positive"], 0)

    def test_reviews_pagination(self):
        company = "company=%D0%A2%D0%B5%D1%81%D1%82-%D0%BA%D0%BE%D0%BC%D0%BF%D0%B0%D0%BD%D0%B8%D1%8F"
        _, first = self.get(f"/api/reviews?{company}&limit=10&offset=0")
        _, second = self.get(f"/api/reviews?{company}&limit=10&offset=10")
        self.assertEqual(len(first["reviews"]), 10)
        self.assertEqual(first["total"], 40)
        self.assertNotEqual(first["reviews"][0]["uid"], second["reviews"][0]["uid"])

    def test_sources_endpoint(self):
        _, data = self.get("/api/sources")
        names = {item["name"] for item in data["sources"]}
        self.assertTrue({"yandex", "2gis", "wildberries", "ozon", "demo"} <= names)

    def test_static_index(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=10) as response:
            body = response.read().decode()
        self.assertEqual(response.status, 200)
        self.assertIn("<title>Отзывы", body)

    def test_unknown_api_route_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_directory_traversal_is_blocked(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/../reviews/server.py")
        self.assertIn(ctx.exception.code, (400, 404))

    def test_collect_endpoint(self):
        payload = json.dumps({"source": "demo", "query": "Новая компания", "limit": 5}).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/collect", data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode())
        self.assertEqual(data["added"], 5)
        self.assertEqual(data["company"], "Новая компания")

    def test_collect_rejects_unknown_source(self):
        payload = json.dumps({"source": "вконтакте", "query": "x"}).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/collect", data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
