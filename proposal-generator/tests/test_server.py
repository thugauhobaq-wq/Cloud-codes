"""Веб-форма: маршруты, отдача PDF, обработка кривых запросов."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer
from urllib.parse import quote

import pytest

from proposals.sample import demo_proposal
from proposals.server import Handler
from proposals.storage import Drafts


@pytest.fixture
def http(tmp_path, family):
    handler = partial(Handler, drafts=Drafts(tmp_path), family=family)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def get(url: str):
    with urllib.request.urlopen(url, timeout=20) as response:
        return response.status, response.headers, response.read()


def post(url: str, payload: dict, method: str = "POST"):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, response.headers, response.read()


def test_serves_the_form(http):
    status, headers, body = get(f"{http}/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"<form" in body


def test_serves_static_assets(http):
    for name, kind in (("style.css", "text/css"), ("app.js", "application/javascript")):
        status, headers, _ = get(f"{http}/{name}")
        assert status == 200
        assert headers["Content-Type"].startswith(kind)


def test_unknown_path_is_404(http):
    with pytest.raises(urllib.error.HTTPError) as error:
        get(f"{http}/{quote('нет-такой-страницы')}")
    assert error.value.code == 404


def test_static_cannot_escape_the_web_root(http):
    """Обход каталога через ../ — первое, что пробуют на локальном сервере."""
    with pytest.raises(urllib.error.HTTPError) as error:
        get(f"{http}/../proposals/server.py")
    assert error.value.code in (400, 404)


def test_state_describes_the_setup(http):
    _, _, body = get(f"{http}/api/state")
    state = json.loads(body)
    assert "classic" in state["templates"]
    assert state["font"]
    assert state["font_error"] is None
    assert state["next_number"] == "1"


def test_demo_endpoint_returns_a_filled_proposal(http):
    _, _, body = get(f"{http}/api/demo")
    demo = json.loads(body)
    assert demo["client"]["name"] == "ООО «Ромашка»"
    assert demo["client"]["logo"], "у демо-заказчика должен быть логотип"
    assert len(demo["items"]) == 5


def test_render_returns_a_pdf(http):
    from proposals import storage

    payload = storage.to_json(demo_proposal())
    status, headers, body = post(f"{http}/api/render", payload)

    assert status == 200
    assert headers["Content-Type"] == "application/pdf"
    assert headers["Content-Disposition"].startswith("inline; filename=")
    assert body.startswith(b"%PDF")
    assert int(headers["Content-Length"]) == len(body)


def test_render_of_an_empty_form_still_works(http):
    status, _, body = post(f"{http}/api/render", {})
    assert status == 200 and body.startswith(b"%PDF")


def test_render_reports_a_broken_logo_in_a_header(http):
    payload = {"client": {"name": "К", "logo": "0J3QtSDQutCw0YDRgtC40L3QutCwCg=="}}
    _, headers, body = post(f"{http}/api/render", payload)
    assert body.startswith(b"%PDF")
    assert "%D0" in headers["X-Proposal-Warnings"], "кириллица в заголовке должна быть в процентах"


def test_bad_json_is_rejected(http):
    request = urllib.request.Request(
        f"{http}/api/render",
        data="{это не json".encode(),
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=10)
    assert error.value.code == 400
    assert "не разобрать JSON" in json.loads(error.value.read())["error"]


def test_json_array_is_rejected(http):
    request = urllib.request.Request(
        f"{http}/api/render", data=b"[1,2]", headers={"Content-Type": "application/json"}
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=10)
    assert error.value.code == 400


def test_draft_lifecycle(http):
    from proposals import storage

    payload = storage.to_json(demo_proposal())

    status, _, body = post(f"{http}/api/drafts/romashka", payload)
    assert status == 200 and json.loads(body)["saved"] == "romashka"

    _, _, body = get(f"{http}/api/drafts")
    assert [item["slug"] for item in json.loads(body)["drafts"]] == ["romashka"]

    _, _, body = get(f"{http}/api/drafts/romashka")
    assert json.loads(body)["client"]["name"] == "ООО «Ромашка»"

    _, _, body = post(f"{http}/api/drafts/romashka", {}, method="DELETE")
    assert json.loads(body)["deleted"] is True

    _, _, body = get(f"{http}/api/drafts")
    assert json.loads(body)["drafts"] == []


def test_missing_draft_is_404(http):
    with pytest.raises(urllib.error.HTTPError) as error:
        get(f"{http}/api/drafts/{quote('нет-такого')}")
    assert error.value.code == 404


def test_next_number_follows_saved_drafts(http):
    from proposals import storage

    post(f"{http}/api/drafts/kp", storage.to_json(demo_proposal()))
    _, _, body = get(f"{http}/api/state")
    assert json.loads(body)["next_number"] == "48"


def test_unknown_api_method_is_404(http):
    with pytest.raises(urllib.error.HTTPError) as error:
        post(f"{http}/api/{quote('магия')}", {})
    assert error.value.code == 404
