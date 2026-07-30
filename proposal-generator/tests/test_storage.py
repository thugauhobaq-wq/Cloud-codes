"""Черновики: сериализация, каталог, безопасность имён."""

from __future__ import annotations

import base64
import json
from datetime import date

import pytest

from proposals import storage
from proposals.models import LineItem, Party, Proposal
from proposals.sample import demo_logo, demo_proposal
from proposals.storage import Drafts, Record, StorageError


@pytest.fixture
def drafts(tmp_path) -> Drafts:
    return Drafts(tmp_path)


def test_roundtrip_keeps_logos(tmp_path):
    proposal = demo_proposal(date(2026, 7, 28))
    path = storage.save(proposal, tmp_path / "kp.proposal.json")
    restored = storage.load(path).proposal

    assert restored.client.logo == proposal.client.logo
    assert restored.sender.logo == proposal.sender.logo
    assert restored.totals().total == proposal.totals().total
    assert restored.items[2].discount_percent == proposal.items[2].discount_percent


def test_roundtrip_keeps_delivery(tmp_path):
    record = Record(proposal=demo_proposal(date(2026, 7, 28)))
    record.delivery.mark_sent("client@example.com")
    record.delivery.mark_viewed()

    restored = storage.load(storage.save(record, tmp_path / "kp.proposal.json"))
    assert restored.delivery.status == "viewed"
    assert restored.delivery.token == record.delivery.token
    assert [event.kind for event in restored.delivery.events] == ["sent", "viewed"]


def test_a_fresh_file_has_an_empty_delivery(tmp_path):
    path = storage.save(demo_proposal(date(2026, 7, 28)), tmp_path / "kp.proposal.json")
    assert storage.load(path).delivery.status == "draft"


def test_save_is_atomic(tmp_path):
    """Заказчик может открыть ссылку ровно в момент записи — и получить обрезок."""
    path = tmp_path / "kp.proposal.json"
    storage.save(demo_proposal(date(2026, 7, 28)), path)
    before = path.read_text("utf-8")

    record = storage.load(path)
    record.proposal.subject = "Другая тема"
    storage.save(record, path)

    assert path.read_text("utf-8") != before
    assert not list(tmp_path.glob(".*tmp*")), "временный файл должен быть убран"


def test_saved_file_is_readable_json(tmp_path):
    """Черновик должен открываться руками — иначе им нельзя обменяться."""
    path = storage.save(demo_proposal(date(2026, 7, 28)), tmp_path / "kp.json")
    raw = json.loads(path.read_text("utf-8"))
    assert raw["client"]["name"] == "ООО «Ромашка»"
    assert "Ромашка" in path.read_text("utf-8"), "кириллица не должна превращаться в \\u"


def test_logo_accepts_data_url():
    encoded = base64.b64encode(demo_logo("A")).decode()
    record = storage.from_json({"client": {"logo": f"data:image/png;base64,{encoded}"}})
    assert record.proposal.client.logo == demo_logo("A")


def test_broken_logo_becomes_none():
    record = storage.from_json({"client": {"logo": "###не base64###"}})
    assert record.proposal.client.logo in (None, b"")


def test_load_reports_bad_json(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{не json", "utf-8")
    with pytest.raises(StorageError, match="это не JSON"):
        storage.load(path)


def test_load_reports_missing_file(tmp_path):
    with pytest.raises(StorageError, match="не читается"):
        storage.load(tmp_path / "нет-такого.json")


def test_load_rejects_non_object(tmp_path):
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]", "utf-8")
    with pytest.raises(StorageError, match="ожидался объект"):
        storage.load(path)


# --- каталог черновиков --------------------------------------------------------


def make(number: str, client: str) -> Proposal:
    return Proposal(
        number=number,
        client=Party(name=client),
        sender=Party(name="Я"),
        items=[LineItem(title="Работа", price=100)],
    )


def test_write_read_list_delete(drafts):
    drafts.write("romashka", make("7", "ООО «Ромашка»"))
    listing = drafts.list()

    assert [info.slug for info in listing] == ["romashka"]
    assert listing[0].number == "7"
    assert listing[0].client == "ООО «Ромашка»"
    assert listing[0].status == "draft"
    assert drafts.read("romashka").proposal.number == "7"

    assert drafts.delete("romashka") is True
    assert drafts.list() == []
    assert drafts.delete("romashka") is False


def test_list_is_newest_first(drafts):
    import os
    import time

    drafts.write("first", make("1", "А"))
    drafts.write("second", make("2", "Б"))
    # Мтime на быстрой машине совпадает — задаём явно.
    path = drafts.path_for("second")
    os.utime(path, (time.time() + 10, time.time() + 10))
    assert [info.slug for info in drafts.list()] == ["second", "first"]


def test_list_skips_foreign_files(drafts, tmp_path):
    drafts.write("ok", make("1", "А"))
    (tmp_path / "мусор.proposal.json").write_text("не json", "utf-8")
    assert [info.slug for info in drafts.list()] == ["ok"]


def test_next_number_grows(drafts):
    assert drafts.next_number() == "1"
    drafts.write("a", make("7", "А"))
    drafts.write("b", make("КП-12", "Б"))
    assert drafts.next_number() == "13"


def test_slug_cannot_escape_the_directory(drafts):
    """Имя черновика приходит из веб-формы — путь наружу собирать нельзя."""
    path = drafts.path_for("../../etc/passwd")
    assert path.parent == drafts.directory
    assert "etc" not in path.name or path.name.startswith("etcpasswd")


def test_empty_slug_is_rejected(drafts):
    with pytest.raises(StorageError, match="пустое имя"):
        drafts.path_for("///")


def test_list_of_missing_directory_is_empty(tmp_path):
    assert Drafts(tmp_path / "нет").list() == []


def test_list_shows_status_and_total(drafts):
    record = Record(proposal=make("7", "Ромашка"))
    record.delivery.mark_sent("a@example.com")
    drafts.write("kp", record)

    info = drafts.list()[0]
    assert info.status == "sent"
    assert info.status_label == "отправлено"
    assert info.total.startswith("100,00")
    assert info.sent_at


def test_find_by_token(drafts):
    record = Record(proposal=make("7", "Ромашка"))
    token = record.delivery.ensure_token()
    drafts.write("kp", record)
    drafts.write("other", make("8", "Вторая"))

    found = drafts.find_by_token(token)
    assert found is not None
    slug, loaded = found
    assert slug == "kp"
    assert loaded.proposal.number == "7"


def test_find_by_token_rejects_wrong_and_empty(drafts):
    record = Record(proposal=make("7", "Ромашка"))
    record.delivery.ensure_token()
    drafts.write("kp", record)

    assert drafts.find_by_token("") is None
    assert drafts.find_by_token("не тот токен") is None


def test_draft_without_a_token_is_not_reachable_by_link(drafts):
    """Пустой токен не должен совпадать с пустым токеном в запросе."""
    drafts.write("kp", make("7", "Ромашка"))
    assert drafts.find_by_token("") is None


def test_update_applies_a_change(drafts):
    drafts.write("kp", make("7", "Ромашка"))
    record = drafts.update("kp", lambda item: item.delivery.mark_sent("a@example.com"))

    assert record.delivery.status == "sent"
    assert drafts.read("kp").delivery.status == "sent"


def test_concurrent_updates_do_not_lose_events(drafts):
    """Форма сохраняет правку ровно тогда, когда заказчик открывает ссылку."""
    import threading

    drafts.write("kp", make("7", "Ромашка"))
    drafts.update("kp", lambda item: item.delivery.mark_sent("a@example.com"))

    def touch() -> None:
        for _ in range(20):
            drafts.update("kp", lambda item: item.delivery.events.append(
                __import__("proposals.delivery", fromlist=["Event"]).Event("viewed")
            ))

    threads = [threading.Thread(target=touch) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(drafts.read("kp").delivery.events) == 1 + 80
