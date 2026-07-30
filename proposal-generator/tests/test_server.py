"""Веб-форма: маршруты, отдача PDF, обработка кривых запросов."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer
from urllib.parse import quote, unquote

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


# --- отправка и публичная страница ---------------------------------------------


@pytest.fixture
def public(tmp_path, family):
    """Сервер с заданным публичным адресом — как при боевом запуске."""
    drafts = Drafts(tmp_path)
    handler = partial(
        Handler, drafts=drafts, family=family, public_url="https://kp.example.test"
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", drafts
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def saved_draft(drafts, slug="kp"):
    from proposals.storage import Record

    record = Record(proposal=demo_proposal())
    drafts.write(slug, record)
    return record


def test_send_requires_a_saved_draft(public):
    base, _ = public
    with pytest.raises(urllib.error.HTTPError) as error:
        post(f"{base}/api/send", {"to": "c@example.com"})
    assert error.value.code == 400
    assert "сохраните" in json.loads(error.value.read())["error"]


def test_send_of_a_missing_draft_is_404(public):
    base, _ = public
    with pytest.raises(urllib.error.HTTPError) as error:
        post(f"{base}/api/send", {"slug": "нет-такого", "to": "c@example.com"})
    assert error.value.code == 404


def test_dry_run_returns_the_letter_and_sends_nothing(public):
    base, drafts = public
    saved_draft(drafts)

    _, _, body = post(
        f"{base}/api/send", {"slug": "kp", "to": "c@example.com", "dry_run": True}
    )
    answer = json.loads(body)

    assert answer["dry_run"] is True
    assert answer["to"] == "c@example.com"
    assert "Коммерческое предложение № 47" in answer["subject"]
    assert "https://kp.example.test/p/" in answer["body"]
    assert drafts.read("kp").delivery.status == "draft", "пробный прогон не меняет статус"


def test_send_without_smtp_explains_itself(public):
    base, drafts = public
    saved_draft(drafts)
    with pytest.raises(urllib.error.HTTPError) as error:
        post(f"{base}/api/send", {"slug": "kp", "to": "c@example.com"})
    assert error.value.code == 400
    assert "не настроена" in json.loads(error.value.read())["error"]


def test_public_page_flow(public):
    """Полный путь заказчика: открыл ссылку, посмотрел PDF, ответил."""
    base, drafts = public
    record = saved_draft(drafts)
    token = record.delivery.ensure_token()
    drafts.write("kp", record)

    status, headers, body = get(f"{base}/p/{token}")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"view.js" in body

    _, _, body = get(f"{base}/p/{token}/data")
    data = json.loads(body)
    assert data["client"] == "ООО «Ромашка»"
    assert data["total"].startswith("318")
    assert len(data["items"]) == 5
    assert drafts.read("kp").delivery.status == "viewed"

    status, headers, body = get(f"{base}/p/{token}/pdf")
    assert status == 200
    assert headers["Content-Type"] == "application/pdf"
    assert body.startswith(b"%PDF")

    _, _, body = post(
        f"{base}/p/{token}/decision", {"accept": True, "comment": "берём"}
    )
    assert json.loads(body)["status"] == "accepted"

    state = drafts.read("kp").delivery
    assert state.status == "accepted"
    assert state.comment == "берём"
    assert [event.kind for event in state.events] == ["viewed", "accepted"]


def test_public_data_never_leaks_the_token_or_recipient(public):
    base, drafts = public
    record = saved_draft(drafts)
    record.delivery.mark_sent("client@example.com")
    token = record.delivery.token
    drafts.write("kp", record)

    _, _, body = get(f"{base}/p/{token}/data")
    dumped = body.decode("utf-8")
    assert token not in dumped
    assert "client@example.com" not in dumped


def test_public_page_does_not_expose_other_proposals(public):
    base, drafts = public
    first = saved_draft(drafts, "first")
    token = first.delivery.ensure_token()
    drafts.write("first", first)

    second = saved_draft(drafts, "second")
    second.proposal.client.name = "Секретный клиент"
    drafts.write("second", second)

    _, _, body = get(f"{base}/p/{token}/data")
    assert "Секретный клиент" not in body.decode("utf-8")


@pytest.mark.parametrize("token", ["net-takogo", quote("кириллица"), "x" * 300])
def test_unknown_token_is_a_polite_404(public, token):
    """Кириллица в токене не должна ронять сервер: ждём 404, а не 500."""
    base, _ = public
    with pytest.raises(urllib.error.HTTPError) as error:
        get(f"{base}/p/{token}")
    assert error.value.code == 404
    assert "Ссылка не действует" in error.value.read().decode("utf-8")


def test_decision_on_an_unknown_token_is_404(public):
    base, _ = public
    with pytest.raises(urllib.error.HTTPError) as error:
        post(f"{base}/p/{quote('нет')}/decision", {"accept": True})
    assert error.value.code == 404


def test_decision_comment_is_capped(public):
    base, drafts = public
    record = saved_draft(drafts)
    token = record.delivery.ensure_token()
    drafts.write("kp", record)

    post(f"{base}/p/{token}/decision", {"accept": False, "comment": "я" * 5000})
    assert len(drafts.read("kp").delivery.comment) == 2000


def test_saving_a_draft_does_not_reset_its_delivery(public):
    """Форма шлёт весь черновик целиком — статус и токен обязан хранить сервер."""
    from proposals import storage

    base, drafts = public
    record = saved_draft(drafts)
    record.delivery.mark_sent("c@example.com")
    token = record.delivery.token
    drafts.write("kp", record)

    payload = storage.to_json(record)
    payload["delivery"] = {"status": "accepted", "token": "подделка"}
    post(f"{base}/api/drafts/kp", payload)

    state = drafts.read("kp").delivery
    assert state.status == "sent", "статус из формы принимать нельзя"
    assert state.token == token


def test_draft_endpoint_hides_the_token(public):
    base, drafts = public
    record = saved_draft(drafts)
    record.delivery.mark_sent("c@example.com")
    drafts.write("kp", record)

    _, _, body = get(f"{base}/api/drafts/kp")
    data = json.loads(body)
    assert "token" not in data["delivery"]
    assert data["delivery"]["link"].startswith("https://kp.example.test/p/")
    assert data["delivery"]["events"][0]["label"] == "отправлено"


def test_funnel(public):
    base, drafts = public
    saved_draft(drafts, "draft-one")

    sent = saved_draft(drafts, "sent-one")
    sent.delivery.mark_sent("c@example.com")
    drafts.write("sent-one", sent)

    won = saved_draft(drafts, "won")
    won.delivery.mark_sent("c@example.com")
    won.delivery.decide(True)
    drafts.write("won", won)

    _, _, body = get(f"{base}/api/funnel")
    funnel = json.loads(body)

    assert funnel["total"] == 3
    assert funnel["delivered"] == 2
    assert funnel["counts"]["accepted"] == 1
    assert funnel["conversion"] == 50.0
    assert funnel["money"]["RUB"]["accepted"].startswith("318")


def test_state_reports_public_url_and_smtp(public):
    base, _ = public
    _, _, body = get(f"{base}/api/state")
    state = json.loads(body)
    assert state["public_url"] == "https://kp.example.test"
    assert state["smtp"]["configured"] is False


def test_smtp_settings_round_trip_without_leaking_the_password(public):
    base, _ = public
    _, _, body = post(
        f"{base}/api/smtp",
        {"host": "smtp.example.test", "user": "me@example.test", "password": "секрет"},
    )
    saved = json.loads(body)
    assert saved["configured"] is True
    assert "секрет" not in body.decode("utf-8")

    _, _, body = get(f"{base}/api/smtp")
    assert "секрет" not in body.decode("utf-8")
    assert json.loads(body)["has_password"] is True


def test_empty_password_keeps_the_stored_one(public):
    """В форме пароль не показывается, поэтому пустое поле — «не трогать»."""
    from proposals import mailer

    base, drafts = public
    post(f"{base}/api/smtp", {"host": "h", "user": "u@e.ru", "password": "секрет"})
    post(f"{base}/api/smtp", {"host": "h2", "user": "u@e.ru", "password": ""})

    assert mailer.load_config(drafts.directory).password == "секрет"
    assert mailer.load_config(drafts.directory).host == "h2"


# --- доступ снаружи ------------------------------------------------------------


def test_admin_routes_need_a_key_when_a_key_is_set(tmp_path, family):
    """С внешнего адреса форма отдаёт черновики и умеет слать почту — нужен ключ."""
    handler = partial(Handler, drafts=Drafts(tmp_path), family=family, admin_token="секрет-ключ")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        # С localhost пускаем и без ключа — иначе своей же формой не попользуешься.
        assert get(f"{base}/api/state")[0] == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_public_routes_stay_open_when_a_key_is_set(tmp_path, family):
    drafts = Drafts(tmp_path)
    record = saved_draft(drafts)
    token = record.delivery.ensure_token()
    drafts.write("kp", record)

    handler = partial(Handler, drafts=drafts, family=family, admin_token="секрет-ключ")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert get(f"{base}/p/{token}")[0] == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_is_loopback():
    from proposals.server import is_loopback

    assert is_loopback("127.0.0.1")
    assert is_loopback("::1")
    assert is_loopback("localhost")
    assert not is_loopback("0.0.0.0")
    assert not is_loopback("192.168.1.10")


# --- счёт и напоминания --------------------------------------------------------


def billable(drafts, slug="kp"):
    """Черновик с реквизитами, по которому можно выставить счёт."""
    from proposals.models import Bank
    from proposals.storage import Record

    record = Record(proposal=demo_proposal())
    record.proposal.sender.inn = "771234567890"
    record.proposal.sender.bank = Bank(
        name="АО «Т-Банк»", bik="044525974", account="40802810100000001234"
    )
    drafts.write(slug, record)
    return record


def test_invoice_from_a_saved_draft(public):
    base, drafts = public
    billable(drafts)

    status, headers, body = post(f"{base}/api/invoice", {"slug": "kp"})
    assert status == 200
    assert headers["Content-Type"] == "application/pdf"
    assert headers["Content-Disposition"].startswith('inline; filename="Schet-')
    assert body.startswith(b"%PDF")
    assert "X-Proposal-Warnings" not in headers


def test_invoice_from_the_form_without_saving(public):
    """Счёт по тому, что сейчас в полях, — черновик для этого не обязателен."""
    from proposals import storage

    base, _ = public
    payload = storage.to_json(demo_proposal())
    status, headers, body = post(f"{base}/api/invoice", payload)

    assert status == 200 and body.startswith(b"%PDF")
    assert "X-Proposal-Warnings" not in headers


def test_invoice_warns_about_missing_requisites(public):
    """Счёт всё равно отдаём — реквизиты могут вписать руками."""
    from proposals import storage
    from proposals.models import LineItem, Party, Proposal

    base, _ = public
    payload = storage.to_json(
        Proposal(sender=Party(name="ИП Иванов"), client=Party(name="ООО «Рога»"),
                 items=[LineItem(title="Работа", price=100)])
    )
    status, headers, body = post(f"{base}/api/invoice", payload)

    assert status == 200 and body.startswith(b"%PDF")
    assert "банковские реквизиты" in unquote(headers["X-Proposal-Warnings"])


def test_invoice_of_a_missing_draft_is_404(public):
    base, _ = public
    with pytest.raises(urllib.error.HTTPError) as error:
        post(f"{base}/api/invoice", {"slug": "нет-такого"})
    assert error.value.code == 404


def test_invoice_number_can_be_overridden(public):
    base, drafts = public
    billable(drafts)
    _, _, plain = post(f"{base}/api/invoice", {"slug": "kp"})
    _, _, renumbered = post(f"{base}/api/invoice", {"slug": "kp", "number": "СЧ-100"})
    assert plain != renumbered


def test_remind_requires_a_sent_proposal(public):
    base, drafts = public
    billable(drafts)
    with pytest.raises(urllib.error.HTTPError) as error:
        post(f"{base}/api/remind", {"slug": "kp"})
    assert error.value.code == 400
    assert "напоминать не о чем" in json.loads(error.value.read())["error"]


def test_remind_dry_run_shows_the_letter(public):
    base, drafts = public
    record = billable(drafts)
    record.delivery.mark_sent("client@example.com")
    drafts.write("kp", record)

    _, _, body = post(f"{base}/api/remind", {"slug": "kp", "dry_run": True})
    answer = json.loads(body)

    assert answer["to"] == "client@example.com"
    assert answer["subject"].startswith("Напоминание")
    assert "https://kp.example.test/p/" in answer["body"]
    assert drafts.read("kp").delivery.reminders == 0


def test_remind_without_a_draft_is_rejected(public):
    base, _ = public
    with pytest.raises(urllib.error.HTTPError) as error:
        post(f"{base}/api/remind", {})
    assert error.value.code == 400


def test_draft_list_reports_silence(public):
    from datetime import timedelta

    from proposals.delivery import now

    base, drafts = public
    record = billable(drafts)
    record.delivery.mark_sent("client@example.com")
    record.delivery.sent_at = now() - timedelta(days=9)
    drafts.write("kp", record)

    _, _, body = get(f"{base}/api/drafts")
    assert json.loads(body)["drafts"][0]["silent_days"] == 9

    _, _, body = get(f"{base}/api/drafts/kp")
    assert json.loads(body)["delivery"]["silent_days"] == 9
