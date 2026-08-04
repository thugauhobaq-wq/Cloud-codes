"""Очередь, хранилище и HTTP-интерфейс."""

from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from conftest import FakeTranslator

from pdftr.jobs import Queue, Settings
from pdftr.server import Application, Handler, _read_multipart, clean_name
from pdftr.store import DONE, FAILED, QUEUED, Store

# --- имена файлов ----------------------------------------------------------


def test_clean_name():
    assert clean_name("отчёт.pdf") == "отчёт.pdf"
    assert clean_name("/etc/passwd") == "passwd.pdf"
    assert clean_name("../../secret.pdf") == "secret.pdf"
    assert clean_name("a" * 300).endswith(".pdf")
    assert clean_name("") == "документ.pdf"
    assert clean_name("файл;rm -rf.pdf").endswith(".pdf")


# --- разбор multipart ------------------------------------------------------


def multipart(fields: dict[str, str], filename: str, payload: bytes) -> tuple[bytes, str]:
    boundary = "----pdftrtest"
    parts = []
    for key, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{filename}"\r\nContent-Type: application/pdf\r\n\r\n'.encode()
        + payload
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def test_multipart_extracts_file_and_fields(tmp_path):
    payload = b"%PDF-1.4\n" + bytes(range(256)) * 40
    body, content_type = multipart({"source": "en", "target": "de"}, "книга.pdf", payload)
    target = tmp_path / "out.bin"
    fields, filename = _read_multipart(io.BytesIO(body), content_type, len(body), target)
    assert fields == {"source": "en", "target": "de"}
    assert filename == "книга.pdf"
    assert target.read_bytes() == payload


def test_multipart_handles_boundary_split_across_chunks(tmp_path, monkeypatch):
    import pdftr.server as server

    monkeypatch.setattr(server, "CHUNK", 8)  # крошечные куски: граница рвётся
    payload = b"%PDF-" + b"x" * 500
    body, content_type = multipart({}, "a.pdf", payload)
    target = tmp_path / "out.bin"
    _read_multipart(io.BytesIO(body), content_type, len(body), target)
    assert target.read_bytes() == payload


def test_multipart_without_boundary_is_an_error(tmp_path):
    with pytest.raises(ValueError):
        _read_multipart(io.BytesIO(b""), "multipart/form-data", 0, tmp_path / "x")


# --- хранилище -------------------------------------------------------------


def test_store_round_trip(tmp_path):
    store = Store(tmp_path)
    job = store.create("книга.pdf", "en", "ru", 1234)
    assert store.get(job.id).name == "книга.pdf"
    store.update(job.id, status=DONE, percent=100, warnings=["мелочь"])
    again = store.get(job.id)
    assert again.status == DONE and again.warnings == ["мелочь"]
    assert [item.id for item in store.recent()] == [job.id]
    store.close()


def test_store_deletes_files_too(tmp_path):
    store = Store(tmp_path)
    job = store.create("a.pdf", "en", "ru", 1)
    store.source_path(job.id).write_bytes(b"%PDF")
    store.result_path(job.id).write_bytes(b"%PDF")
    assert store.delete(job.id)
    assert not store.source_path(job.id).exists()
    assert not store.result_path(job.id).exists()
    assert store.get(job.id) is None
    store.close()


def test_pending_returns_unfinished_jobs(tmp_path):
    store = Store(tmp_path)
    waiting = store.create("a.pdf", "en", "ru", 1)
    finished = store.create("b.pdf", "en", "ru", 1)
    store.update(finished.id, status=DONE)
    assert [item.id for item in store.pending()] == [waiting.id]
    store.close()


def test_sweep_removes_old_finished_jobs(tmp_path):
    store = Store(tmp_path)
    for index in range(5):
        job = store.create(f"{index}.pdf", "en", "ru", 1)
        store.update(job.id, status=DONE, finished=time.time() - 3600 * 100)
    assert store.sweep(keep_hours=1, keep_count=2) == 3
    assert len(store.recent()) == 2
    store.close()


def test_sweep_keeps_fresh_and_unfinished_jobs(tmp_path):
    store = Store(tmp_path)
    fresh = store.create("fresh.pdf", "en", "ru", 1)
    store.update(fresh.id, status=DONE, finished=time.time())
    waiting = store.create("waiting.pdf", "en", "ru", 1)
    assert store.sweep(keep_hours=1, keep_count=0) == 0
    assert {item.id for item in store.recent()} == {fresh.id, waiting.id}
    store.close()


# --- очередь ---------------------------------------------------------------


def wait_for(check, timeout: float = 20.0):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("не дождались")


@pytest.fixture()
def queue(tmp_path, monkeypatch):
    """Очередь с заглушкой переводчика вместо сети."""
    import pdftr.jobs as jobs_module

    monkeypatch.setattr(jobs_module, "GoogleTranslator", lambda **_kwargs: FakeTranslator())
    store = Store(tmp_path)
    queue = Queue(store, Settings(workers=1, rate=0))
    queue.start()
    yield queue
    queue.stop()
    store.close()


def test_queue_translates_a_file(queue, simple_pdf, tmp_path):
    upload = tmp_path / "in.pdf"
    upload.write_bytes(simple_pdf)
    job = queue.submit("book.pdf", "en", "ru", upload)
    done = wait_for(lambda: queue.store.get(job.id).status == DONE and queue.store.get(job.id))
    assert done.pages == 1
    assert done.percent == 100
    assert queue.store.result_path(job.id).read_bytes().startswith(b"%PDF")


def test_queue_reports_progress_to_listeners(queue, simple_pdf, tmp_path):
    seen: list[dict] = []
    upload = tmp_path / "in.pdf"
    upload.write_bytes(simple_pdf)
    job = queue.store.create("book.pdf", "en", "ru", len(simple_pdf))
    queue.store.source_path(job.id).write_bytes(simple_pdf)
    queue.listen(job.id, seen.append)
    queue._queue.put(job.id)
    wait_for(lambda: seen and seen[-1]["status"] == DONE)
    assert next(item["status"] for item in seen) == "running"


def test_queue_fails_gracefully_on_garbage(queue, tmp_path):
    upload = tmp_path / "in.pdf"
    upload.write_bytes(b"%PDF-1.4 but broken beyond repair")
    job = queue.submit("junk.pdf", "en", "ru", upload)
    failed = wait_for(lambda: queue.store.get(job.id).status == FAILED and queue.store.get(job.id))
    assert failed.error


def test_queue_cancels_a_waiting_job(queue, simple_pdf, tmp_path):
    upload = tmp_path / "in.pdf"
    upload.write_bytes(simple_pdf)
    job = queue.store.create("book.pdf", "en", "ru", 1)
    assert queue.cancel(job.id)
    assert queue.store.get(job.id).status == "cancelled"


def test_restart_puts_interrupted_jobs_back(tmp_path, monkeypatch, simple_pdf):
    import pdftr.jobs as jobs_module

    monkeypatch.setattr(jobs_module, "GoogleTranslator", lambda **_kwargs: FakeTranslator())
    store = Store(tmp_path)
    job = store.create("book.pdf", "en", "ru", len(simple_pdf))
    store.source_path(job.id).write_bytes(simple_pdf)
    store.update(job.id, status="running", percent=42)  # как будто сервер упал

    queue = Queue(store, Settings(workers=1, rate=0))
    queue.start()
    try:
        wait_for(lambda: store.get(job.id).status == DONE)
    finally:
        queue.stop()
        store.close()


# --- HTTP ------------------------------------------------------------------


@pytest.fixture()
def http(tmp_path, monkeypatch):
    import pdftr.jobs as jobs_module

    monkeypatch.setattr(jobs_module, "GoogleTranslator", lambda **_kwargs: FakeTranslator())
    app = Application(tmp_path, settings=Settings(workers=1, rate=0), max_upload=1024 * 1024)
    app.start()
    handler = type("Bound", (Handler,), {"app": app})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, app
    server.shutdown()
    app.stop()


def fetch(url: str, data: bytes | None = None, method: str = "GET", headers=None):
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.status, response.read(), dict(response.headers)


def test_index_and_static_files(http):
    base, _app = http
    status, body, headers = fetch(base + "/")
    assert status == 200 and b"<title>" in body
    assert "text/html" in headers["Content-Type"]
    assert fetch(base + "/app.js")[0] == 200
    assert fetch(base + "/style.css")[0] == 200
    assert fetch(base + "/manifest.webmanifest")[0] == 200
    assert fetch(base + "/sw.js")[0] == 200


def test_unknown_path_opens_the_application(http):
    base, _app = http
    status, body, _headers = fetch(base + "/no/such/place")
    assert status == 200 and b"<title>" in body


def test_config_lists_languages(http):
    base, _app = http
    payload = json.loads(fetch(base + "/api/config")[1])
    assert payload["languages"]["ru"] == "Русский"
    assert "auto" not in payload["targets"]
    assert payload["maxUpload"] == 1024 * 1024


def test_upload_translate_download(http, simple_pdf):
    base, _app = http
    status, body, _headers = fetch(
        base + "/api/jobs?name=book.pdf&source=en&target=ru",
        data=simple_pdf,
        method="POST",
        headers={"Content-Type": "application/pdf"},
    )
    assert status == 202
    job = json.loads(body)
    assert job["name"] == "book.pdf" and job["status"] == QUEUED

    wait_for(lambda: json.loads(fetch(f"{base}/api/jobs/{job['id']}")[1])["status"] == DONE)
    status, data, headers = fetch(f"{base}/api/jobs/{job['id']}/file")
    assert status == 200
    assert data.startswith(b"%PDF")
    assert "attachment" in headers["Content-Disposition"]
    assert "book%20%28ru%29.pdf" in headers["Content-Disposition"]

    assert fetch(f"{base}/api/jobs/{job['id']}/source")[1] == simple_pdf


def test_download_before_it_is_ready(http, simple_pdf):
    base, app = http
    job = app.store.create("book.pdf", "en", "ru", 10)
    with pytest.raises(urllib.error.HTTPError) as error:
        fetch(f"{base}/api/jobs/{job.id}/file")
    assert error.value.code == 409


def test_upload_that_is_not_a_pdf(http):
    base, _app = http
    with pytest.raises(urllib.error.HTTPError) as error:
        fetch(base + "/api/jobs?name=a.pdf", data=b"just text" * 20, method="POST",
              headers={"Content-Type": "application/pdf"})
    assert error.value.code == 415


def test_upload_too_big(http, simple_pdf):
    base, _app = http
    with pytest.raises(urllib.error.HTTPError) as error:
        fetch(base + "/api/jobs?name=a.pdf", data=b"%PDF" + b"x" * (2 * 1024 * 1024),
              method="POST", headers={"Content-Type": "application/pdf"})
    assert error.value.code == 413


def test_share_target_redirects_back(http, simple_pdf):
    base, _app = http
    body, content_type = multipart({"target": "de"}, "shared.pdf", simple_pdf)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(
        base + "/share", data=body, method="POST", headers={"Content-Type": content_type}
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        opener.open(request, timeout=20)
    assert error.value.code == 303
    assert error.value.headers["Location"].startswith("/?job=")


def test_job_list_and_delete(http, simple_pdf):
    base, app = http
    job = app.store.create("book.pdf", "en", "ru", 1)
    listed = json.loads(fetch(base + "/api/jobs")[1])
    assert any(item["id"] == job.id for item in listed)
    assert json.loads(fetch(f"{base}/api/jobs/{job.id}", method="DELETE")[1])["deleted"]
    assert app.store.get(job.id) is None


def test_missing_job_is_404(http):
    base, _app = http
    with pytest.raises(urllib.error.HTTPError) as error:
        fetch(base + "/api/jobs/deadbeefdeadbeef")
    assert error.value.code == 404


def test_events_stream_reports_the_finish(http, simple_pdf):
    base, _app = http
    _status, body, _headers = fetch(
        base + "/api/jobs?name=book.pdf&source=en&target=ru",
        data=simple_pdf, method="POST", headers={"Content-Type": "application/pdf"},
    )
    job_id = json.loads(body)["id"]
    with urllib.request.urlopen(f"{base}/api/jobs/{job_id}/events", timeout=25) as stream:
        assert stream.headers["Content-Type"].startswith("text/event-stream")
        seen = []
        for raw in stream:
            line = raw.decode("utf-8").strip()
            if line.startswith("data: "):
                payload = json.loads(line[6:])
                seen.append(payload["status"])
                if payload["status"] == DONE:
                    break
    assert seen[-1] == DONE
