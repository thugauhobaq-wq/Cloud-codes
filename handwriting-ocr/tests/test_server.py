"""HTTP: загрузка, статус, правка текста, превью, «Поделиться»."""

from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from conftest import HEIC, JPEG, PNG, WEBP, FakeRecognizer, wait_for

from hwocr.jobs import Settings
from hwocr.server import Application, Handler, _read_multipart, clean_name
from hwocr.store import DONE, QUEUED

# --- имена файлов ----------------------------------------------------------


def test_clean_name():
    assert clean_name("лист.jpg") == "лист.jpg"
    assert clean_name("IMG_0001.HEIC", "image/jpeg") == "IMG_0001.jpg"
    assert clean_name("/etc/passwd", "image/png") == "passwd.png"
    assert clean_name("../../secret.jpeg", "image/webp") == "secret.webp"
    assert clean_name("") == "фото.jpg"
    assert clean_name("a" * 300).endswith(".jpg")


# --- разбор multipart ------------------------------------------------------


def multipart(fields: dict[str, str], filename: str, payload: bytes, field="image"):
    boundary = "----hwocrtest"
    parts = []
    for key, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
        f'filename="{filename}"\r\nContent-Type: image/jpeg\r\n\r\n'.encode()
        + payload
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def test_multipart_extracts_file_and_fields(tmp_path):
    body, content_type = multipart({"title": "заметка"}, "лист.jpg", JPEG)
    target = tmp_path / "out.bin"
    fields, filename = _read_multipart(io.BytesIO(body), content_type, len(body), target)
    assert fields == {"title": "заметка"} and filename == "лист.jpg"
    assert target.read_bytes() == JPEG


def test_multipart_handles_boundary_split_across_chunks(tmp_path, monkeypatch):
    import hwocr.server as server

    monkeypatch.setattr(server, "CHUNK", 8)  # крошечные куски: граница рвётся
    body, content_type = multipart({}, "a.jpg", JPEG)
    target = tmp_path / "out.bin"
    _read_multipart(io.BytesIO(body), content_type, len(body), target)
    assert target.read_bytes() == JPEG


def test_multipart_without_boundary_is_an_error(tmp_path):
    with pytest.raises(ValueError):
        _read_multipart(io.BytesIO(b""), "multipart/form-data", 0, tmp_path / "x")


# --- HTTP ------------------------------------------------------------------


@pytest.fixture()
def http(tmp_path):
    engine = FakeRecognizer("Распознанный\nтекст")
    app = Application(
        tmp_path, settings=Settings(keep_hours=1, keep_count=10),
        max_upload=64 * 1024, recognizer=engine,
    )
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


def upload(base: str, data: bytes = JPEG, name: str = "лист.jpg") -> dict:
    status, body, _headers = fetch(
        f"{base}/api/jobs?name={urllib.request.quote(name)}",
        data=data, method="POST", headers={"Content-Type": "application/octet-stream"},
    )
    assert status == 202
    return json.loads(body)


def status_of(base: str, job_id: str) -> dict:
    return json.loads(fetch(f"{base}/api/jobs/{job_id}")[1])


def test_index_and_static_files(http):
    base, _app = http
    status, body, headers = fetch(base + "/")
    assert status == 200 and b"<title>" in body
    assert "text/html" in headers["Content-Type"]
    for path in ("/app.js", "/style.css", "/manifest.webmanifest", "/sw.js", "/icon.svg"):
        assert fetch(base + path)[0] == 200, path
    assert fetch(base + "/no/such/place")[1] == body, "неизвестный адрес открывает приложение"


def test_config_describes_the_engine(http):
    base, _app = http
    payload = json.loads(fetch(base + "/api/config")[1])
    assert payload["engine"] == "fake"
    assert payload["maxUpload"] == 64 * 1024
    assert payload["accept"] == ["image/jpeg", "image/png", "image/webp"]


@pytest.mark.parametrize(
    ("data", "media"), [(JPEG, "image/jpeg"), (PNG, "image/png"), (WEBP, "image/webp")]
)
def test_upload_recognize_read(http, data, media):
    base, _app = http
    job = upload(base, data)
    assert job["status"] == QUEUED and job["mediaType"] == media and job["engine"] == "fake"
    wait_for(lambda: status_of(base, job["id"])["status"] == DONE)
    done = status_of(base, job["id"])
    assert done["text"] == "Распознанный\nтекст" and done["edited"] is False


def test_upload_that_is_not_a_photo(http):
    base, _app = http
    with pytest.raises(urllib.error.HTTPError) as error:
        fetch(base + "/api/jobs?name=a.jpg", data=b"just text" * 20, method="POST",
              headers={"Content-Type": "image/jpeg"})
    assert error.value.code == 415
    assert "JPEG" in json.loads(error.value.read())["error"]


def test_heic_gets_a_specific_hint(http):
    base, _app = http
    with pytest.raises(urllib.error.HTTPError) as error:
        fetch(base + "/api/jobs?name=IMG.HEIC", data=HEIC, method="POST")
    assert error.value.code == 415
    assert "iPhone" in json.loads(error.value.read())["error"]


def test_upload_too_big(http):
    base, _app = http
    with pytest.raises(urllib.error.HTTPError) as error:
        fetch(base + "/api/jobs?name=a.jpg", data=JPEG + b"x" * (65 * 1024), method="POST")
    assert error.value.code == 413


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def share(base: str, body: bytes, content_type: str) -> urllib.error.HTTPError:
    opener = urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(
        base + "/share", data=body, method="POST", headers={"Content-Type": content_type}
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        opener.open(request, timeout=20)
    return error.value


def test_share_target_redirects_to_the_new_card(http):
    base, app = http
    body, content_type = multipart({"title": "из галереи"}, "photo.jpg", JPEG)
    error = share(base, body, content_type)
    assert error.code == 303 and error.headers["Location"].startswith("/?job=")
    job_id = error.headers["Location"].split("=")[1]
    assert app.store.get(job_id).name == "photo.jpg"


def test_share_target_reports_garbage_on_screen(http):
    base, _app = http
    body, content_type = multipart({}, "doc.pdf", b"%PDF-1.4 nope")
    error = share(base, body, content_type)
    assert error.code == 303 and error.headers["Location"].startswith("/?error=")
    assert "JPEG" in urllib.request.unquote(error.headers["Location"])


def test_edit_text_is_saved(http):
    base, _app = http
    job = upload(base)
    wait_for(lambda: status_of(base, job["id"])["status"] == DONE)
    status, body, _ = fetch(
        f"{base}/api/jobs/{job['id']}/text",
        data=json.dumps({"text": "поправил"}).encode(), method="PUT",
        headers={"Content-Type": "application/json"},
    )
    assert status == 200 and json.loads(body)["edited"] is True
    again = status_of(base, job["id"])
    assert again["text"] == "поправил" and again["edited"] is True


def test_edit_before_ready_is_refused(http):
    base, app = http
    job = app.store.create("a.jpg", "image/jpeg", 1)
    with pytest.raises(urllib.error.HTTPError) as error:
        fetch(f"{base}/api/jobs/{job.id}/text", data=b'{"text": "x"}', method="PUT")
    assert error.value.code == 409


def test_edit_with_garbage_body(http):
    base, app = http
    job = app.store.create("a.jpg", "image/jpeg", 1)
    app.store.update(job.id, status=DONE)
    for body in (b"not json", b'{"text": 5}', b"{}"):
        with pytest.raises(urllib.error.HTTPError) as error:
            fetch(f"{base}/api/jobs/{job.id}/text", data=body, method="PUT")
        assert error.value.code == 400, body


def test_image_preview_has_the_right_type(http):
    base, _app = http
    job = upload(base, PNG, "лист.png")
    status, body, headers = fetch(f"{base}/api/jobs/{job['id']}/image")
    assert status == 200 and body == PNG
    assert headers["Content-Type"] == "image/png"
    assert "private" in headers["Cache-Control"]


def test_missing_job_is_404(http):
    base, _app = http
    for path in ("/api/jobs/deadbeefdeadbeef", "/api/jobs/deadbeefdeadbeef/image"):
        with pytest.raises(urllib.error.HTTPError) as error:
            fetch(base + path)
        assert error.value.code == 404, path


def test_job_list_and_delete(http):
    base, app = http
    job = upload(base)
    listed = json.loads(fetch(base + "/api/jobs")[1])
    assert any(item["id"] == job["id"] for item in listed)
    assert json.loads(fetch(f"{base}/api/jobs/{job['id']}", method="DELETE")[1])["deleted"]
    assert app.store.get(job["id"]) is None
    assert not app.store.image_path(job["id"]).exists()


def test_events_stream_reports_the_finish(http):
    base, _app = http
    job = upload(base)
    with urllib.request.urlopen(f"{base}/api/jobs/{job['id']}/events", timeout=25) as stream:
        assert stream.headers["Content-Type"].startswith("text/event-stream")
        seen = []
        for raw in stream:
            line = raw.decode("utf-8").strip()
            if line.startswith("data: "):
                payload = json.loads(line[6:])
                seen.append(payload["status"])
                if payload["status"] == DONE:
                    assert payload["text"]
                    break
    assert seen[-1] == DONE
