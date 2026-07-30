"""Черновики: сериализация, каталог, безопасность имён."""

from __future__ import annotations

import base64
import json
from datetime import date

import pytest

from proposals import storage
from proposals.models import LineItem, Party, Proposal
from proposals.sample import demo_logo, demo_proposal
from proposals.storage import Drafts, StorageError


@pytest.fixture
def drafts(tmp_path) -> Drafts:
    return Drafts(tmp_path)


def test_roundtrip_keeps_logos(tmp_path):
    proposal = demo_proposal(date(2026, 7, 28))
    path = storage.save(proposal, tmp_path / "kp.proposal.json")
    restored = storage.load(path)

    assert restored.client.logo == proposal.client.logo
    assert restored.sender.logo == proposal.sender.logo
    assert restored.totals().total == proposal.totals().total
    assert restored.items[2].discount_percent == proposal.items[2].discount_percent


def test_saved_file_is_readable_json(tmp_path):
    """Черновик должен открываться руками — иначе им нельзя обменяться."""
    path = storage.save(demo_proposal(date(2026, 7, 28)), tmp_path / "kp.json")
    raw = json.loads(path.read_text("utf-8"))
    assert raw["client"]["name"] == "ООО «Ромашка»"
    assert "Ромашка" in path.read_text("utf-8"), "кириллица не должна превращаться в \\u"


def test_logo_accepts_data_url():
    encoded = base64.b64encode(demo_logo("A")).decode()
    proposal = storage.from_json({"client": {"logo": f"data:image/png;base64,{encoded}"}})
    assert proposal.client.logo == demo_logo("A")


def test_broken_logo_becomes_none():
    proposal = storage.from_json({"client": {"logo": "###не base64###"}})
    assert proposal.client.logo in (None, b"")


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
    assert drafts.read("romashka").number == "7"

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
