from __future__ import annotations

import pytest

from botkit.messaging import TEXT_LIMIT, parts, payload, split_text
from botkit.texts import (
    counted,
    escape,
    human_duration,
    looks_like_phone,
    money,
    normalize_phone,
    plural,
    short,
    translit,
)


def test_escape_protects_markup():
    assert escape("<b>Вася</b>") == "&lt;b&gt;Вася&lt;/b&gt;"
    assert escape(None) == ""


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (1, "заявка"), (2, "заявки"), (4, "заявки"), (5, "заявок"),
        (11, "заявок"), (14, "заявок"), (21, "заявка"), (22, "заявки"),
        (25, "заявок"), (101, "заявка"), (0, "заявок"),
    ],
)
def test_plural(count: int, expected: str):
    assert plural(count, "заявка", "заявки", "заявок") == expected


def test_counted():
    assert counted(5, "заявка", "заявки", "заявок") == "5 заявок"


def test_money():
    assert money(1500) == "1 500 ₽"
    assert money(1234567, "€") == "1 234 567 €"
    assert money(0, "") == "0"


def test_human_duration():
    assert human_duration(90) == "1 ч 30 мин"
    assert human_duration(60) == "1 ч"
    assert human_duration(45) == "45 мин"


def test_short_keeps_readable_length():
    assert short("Короткая строка", 60) == "Короткая строка"
    assert short("а" * 100, 10).endswith("…")
    assert len(short("а" * 100, 10)) == 10


@pytest.mark.parametrize(
    "raw", ["89001234567", "+7 900 123-45-67", "9001234567", "8 (900) 123 45 67"]
)
def test_phone_is_normalised(raw: str):
    """В базе и в уведомлении владельцу номер должен выглядеть одинаково."""
    assert normalize_phone(raw) == "+7 900 123-45-67"


def test_unknown_phone_format_is_kept_as_is():
    assert normalize_phone("+380 44 123 4567") == "+380 44 123 4567"
    assert normalize_phone("") == ""


def test_looks_like_phone():
    assert looks_like_phone("+7 900 123-45-67")
    assert not looks_like_phone("позвоните мне")
    assert not looks_like_phone("12345")


def test_payload_and_parts():
    assert payload("svc:12") == "12"
    assert payload("status:12:done", 2) == "done"
    assert payload("noop") == ""
    assert parts("a:b:c") == ["a", "b", "c"]


def test_short_text_is_not_split():
    assert split_text("Привет") == ["Привет"]


def test_long_text_is_split_by_lines():
    text = "\n".join(f"строка {index}" for index in range(2000))
    chunks = split_text(text)

    assert len(chunks) > 1
    assert all(len(chunk) <= TEXT_LIMIT for chunk in chunks)
    # Ни одна строка не разорвана посередине.
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_a_single_line_longer_than_the_limit_is_cut():
    chunks = split_text("а" * (TEXT_LIMIT + 100))
    assert len(chunks) == 2
    assert len(chunks[0]) == TEXT_LIMIT


def test_translit_gives_a_latin_name_for_a_russian_title():
    assert translit("Магазин у дома") == "magazin u doma"
    assert translit("Щёлочь, объезд") == "scheloch, obezd"
    assert translit("Shop 2024") == "shop 2024"
    assert translit("") == ""
