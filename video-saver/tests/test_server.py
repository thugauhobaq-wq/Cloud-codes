"""Сервер: маршруты, сессия, поток прогресса и отдача файла с перемоткой."""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from conftest import FakeDownloader, wait_status

from vsaver.jobs import Settings
from vsaver.server import Application, Handler
from vsaver.store import DONE, FAILED


class Client:
    """Маленький HTTP-клиент: запоминает cookie, как это делает браузер."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.cookie = ""

    def request(self, method: str, path: str, body=None, headers=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if self.cookie:
            request.add_header("Cookie", self.cookie)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        raw = response.headers.get("Set-Cookie")
        if raw:
            self.cookie = raw.split(";")[0]
        payload = response.read()
        return response, payload

    def json(self, method: str, path: str, body=None, headers=None):
        response, payload = self.request(method, path, body, headers)
        try:
            return response, json.loads(payload.decode("utf-8"))
        except ValueError:
            return response, None


@pytest.fixture
def site(tmp_path, limits):
    """Поднятый сервер с заглушкой вместо загрузчика."""
    downloader = FakeDownloader()
    app = Application(
        tmp_path / "data",
        settings=Settings(workers=1, limits=limits),
        downloader=downloader,
    )
    app.start()
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield {
            "app": app,
            "downloader": downloader,
            "client": Client(f"http://{host}:{port}"),
            "base": f"http://{host}:{port}",
        }
    finally:
        server.shutdown()
        server.server_close()
        app.stop()


def make_job(site, url="https://example.com/v", **extra):
    """Поставить задачу и дождаться, пока она доедет до конца."""
    response, payload = site["client"].json("POST", "/api/jobs", {"url": url, **extra})
    assert response.status == 202, payload
    wait_status(site["app"].store, payload["id"], DONE, FAILED)
    return payload["id"]


class TestPage:
    def test_index_is_served(self, site):
        response, body = site["client"].request("GET", "/")
        assert response.status == 200
        assert b"<title>" in body

    def test_unknown_path_opens_the_page(self, site):
        # Путь в запросе только ASCII: кириллица в адресе едет закодированной,
        # как её и отправляет браузер.
        response, body = site["client"].request("GET", "/%D0%B2%D0%B8%D0%B4%D0%B5%D0%BE")
        assert response.status == 200
        assert b"<title>" in body

    def test_static_files(self, site):
        for path in ("/app.js", "/style.css", "/sw.js", "/manifest.webmanifest", "/icon.svg"):
            response, body = site["client"].request("GET", path)
            assert response.status == 200, path
            assert body

    def test_no_escaping_the_web_directory(self, site):
        """Попытка вылезти из каталога со статикой не должна отдать чужой файл."""
        _, body = site["client"].request("GET", "/../../pyproject.toml")
        assert b"[project]" not in body

    def test_unknown_api_path_is_404(self, site):
        response, _ = site["client"].request("GET", "/api/nothing-here")
        assert response.status == 404


class TestConfig:
    def test_limits_are_published(self, site, limits):
        _, payload = site["client"].json("GET", "/api/config")
        assert payload["limits"]["maxSize"] == limits.max_size
        assert payload["limits"]["perHour"] == limits.per_hour
        assert payload["defaultQuality"] in payload["qualities"]

    def test_no_login_required(self, site):
        """Главное свойство сервиса: страница и API открыты без всякого входа."""
        response, _ = site["client"].request("GET", "/api/config")
        assert response.status == 200


class TestCreate:
    def test_job_is_accepted(self, site):
        response, payload = site["client"].json(
            "POST", "/api/jobs", {"url": "https://example.com/v"}
        )
        assert response.status == 202
        assert payload["status"] == "queued"

    def test_cookie_is_issued(self, site):
        response, _ = site["client"].request(
            "POST", "/api/jobs", {"url": "https://example.com/v"}
        )
        cookie = response.headers.get("Set-Cookie")
        assert cookie and cookie.startswith("vsaver=")
        assert "HttpOnly" in cookie
        assert "SameSite=Lax" in cookie

    def test_bad_url_is_explained(self, site):
        response, payload = site["client"].json("POST", "/api/jobs", {"url": "file:///etc/passwd"})
        assert response.status == 400
        assert "http" in payload["error"]

    def test_private_address_is_refused(self, site):
        response, payload = site["client"].json(
            "POST", "/api/jobs", {"url": "http://169.254.169.254/latest/meta-data/"}
        )
        assert response.status == 400
        assert "внутрь сети" in payload["error"]

    def test_broken_json(self, site):
        request = urllib.request.Request(
            site["base"] + "/api/jobs", data=b"{not json", method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        assert error.value.code == 400

    def test_huge_body_is_refused(self, site):
        request = urllib.request.Request(
            site["base"] + "/api/jobs", data=b"x" * 100_000, method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        assert error.value.code == 413

    def test_unknown_quality_falls_back(self, site):
        _, payload = site["client"].json(
            "POST", "/api/jobs", {"url": "https://example.com/v", "quality": "4320"}
        )
        assert payload["quality"] == "1080"

    def test_rate_limit_answers_429(self, site, limits):
        client = site["client"]
        last = None
        for number in range(limits.per_hour + 2):
            last = client.json("POST", "/api/jobs", {"url": f"https://example.com/{number}"})
            if last[0].status == 429:
                break
        response, payload = last
        assert response.status == 429
        assert response.headers.get("Retry-After")
        assert "час" in payload["error"]


class TestMyJobs:
    def test_own_jobs_are_listed(self, site):
        job_id = make_job(site)
        _, payload = site["client"].json("GET", "/api/jobs")
        assert [job["id"] for job in payload] == [job_id]

    def test_other_sessions_see_nothing(self, site):
        make_job(site)
        stranger = Client(site["base"])
        _, payload = stranger.json("GET", "/api/jobs")
        assert payload == []

    def test_no_cookie_no_list(self, site):
        make_job(site)
        response, payload = Client(site["base"]).json("GET", "/api/jobs")
        assert response.status == 200
        assert payload == []

    def test_card_is_public_by_id(self, site):
        """Ссылку на готовую задачу можно переслать себе на телефон."""
        job_id = make_job(site)
        response, payload = Client(site["base"]).json("GET", f"/api/jobs/{job_id}")
        assert response.status == 200
        assert payload["id"] == job_id

    def test_missing_job(self, site):
        response, _ = site["client"].request("GET", "/api/jobs/deadbeefdeadbeef")
        assert response.status == 404


class TestDelete:
    def test_own_job_is_deleted(self, site):
        job_id = make_job(site)
        response, payload = site["client"].json("DELETE", f"/api/jobs/{job_id}")
        assert response.status == 200 and payload["deleted"]
        assert site["app"].store.get(job_id) is None

    def test_stranger_cannot_delete(self, site):
        """Открытый сервис — не повод давать сносить чужие задачи с улицы."""
        job_id = make_job(site)
        response, _ = Client(site["base"]).json("DELETE", f"/api/jobs/{job_id}")
        assert response.status == 403
        assert site["app"].store.get(job_id) is not None


class TestFile:
    def test_download_has_a_name(self, site):
        job_id = make_job(site)
        response, body = site["client"].request("GET", f"/api/jobs/{job_id}/file")
        assert response.status == 200
        assert response.headers["Content-Type"] == "video/mp4"
        assert "attachment" in response.headers["Content-Disposition"]
        assert len(body) == site["downloader"].size

    def test_inline_for_the_player(self, site):
        job_id = make_job(site)
        response, _ = site["client"].request("GET", f"/api/jobs/{job_id}/file?inline=1")
        assert "inline" in response.headers["Content-Disposition"]

    def test_unfinished_job_has_no_file(self, site):
        store = site["app"].store
        job = store.create("https://example.com/v")
        response, _ = site["client"].request("GET", f"/api/jobs/{job.id}/file")
        assert response.status == 409

    def test_swept_file_says_so(self, site):
        job_id = make_job(site)
        for path in site["app"].store.files.glob(f"{job_id}*"):
            path.unlink()
        response, _ = site["client"].request("GET", f"/api/jobs/{job_id}/file")
        assert response.status == 410


class TestRange:
    """Перемотка в плеере и докачка после обрыва — обе живут на этом заголовке."""

    def test_accept_ranges_is_advertised(self, site):
        job_id = make_job(site)
        response, _ = site["client"].request("GET", f"/api/jobs/{job_id}/file")
        assert response.headers["Accept-Ranges"] == "bytes"

    def test_partial_content(self, site):
        job_id = make_job(site)
        size = site["downloader"].size
        response, body = site["client"].request(
            "GET", f"/api/jobs/{job_id}/file", headers={"Range": "bytes=0-99"}
        )
        assert response.status == 206
        assert response.headers["Content-Range"] == f"bytes 0-99/{size}"
        assert response.headers["Content-Length"] == "100"
        assert len(body) == 100

    def test_open_ended_range(self, site):
        job_id = make_job(site)
        size = site["downloader"].size
        response, body = site["client"].request(
            "GET", f"/api/jobs/{job_id}/file", headers={"Range": "bytes=100-"}
        )
        assert response.status == 206
        assert len(body) == size - 100
        assert response.headers["Content-Range"] == f"bytes 100-{size - 1}/{size}"

    def test_suffix_range(self, site):
        """Плеер первым делом просит хвост файла — там лежит оглавление mp4."""
        job_id = make_job(site)
        size = site["downloader"].size
        response, body = site["client"].request(
            "GET", f"/api/jobs/{job_id}/file", headers={"Range": "bytes=-256"}
        )
        assert response.status == 206
        assert len(body) == 256
        assert response.headers["Content-Range"] == f"bytes {size - 256}-{size - 1}/{size}"

    def test_range_beyond_the_end(self, site):
        job_id = make_job(site)
        size = site["downloader"].size
        response, _ = site["client"].request(
            "GET", f"/api/jobs/{job_id}/file", headers={"Range": f"bytes={size + 10}-"}
        )
        assert response.status == 416
        assert response.headers["Content-Range"] == f"bytes */{size}"

    def test_nonsense_range(self, site):
        job_id = make_job(site)
        response, _ = site["client"].request(
            "GET", f"/api/jobs/{job_id}/file", headers={"Range": "bytes=abc"}
        )
        assert response.status == 416

    def test_range_larger_than_file_is_clamped(self, site):
        job_id = make_job(site)
        size = site["downloader"].size
        response, body = site["client"].request(
            "GET", f"/api/jobs/{job_id}/file", headers={"Range": "bytes=0-999999"}
        )
        assert response.status == 206
        assert len(body) == size


class TestEvents:
    def test_progress_stream_ends_with_the_job(self, site):
        """Поток закрывается сам, когда задача завершилась, — вкладка не висит."""
        _, payload = site["client"].json("POST", "/api/jobs", {"url": "https://example.com/v"})
        request = urllib.request.Request(site["base"] + f"/api/jobs/{payload['id']}/events")
        request.add_header("Cookie", site["client"].cookie)
        with urllib.request.urlopen(request, timeout=10) as stream:
            assert stream.headers["Content-Type"].startswith("text/event-stream")
            body = stream.read().decode("utf-8")
        events = [
            json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")
        ]
        assert events
        assert events[-1]["status"] in (DONE, FAILED)

    def test_stream_for_finished_job_answers_at_once(self, site):
        job_id = make_job(site)
        request = urllib.request.Request(site["base"] + f"/api/jobs/{job_id}/events")
        with urllib.request.urlopen(request, timeout=10) as stream:
            body = stream.read().decode("utf-8")
        assert re.search(r'"status": "done"', body)
