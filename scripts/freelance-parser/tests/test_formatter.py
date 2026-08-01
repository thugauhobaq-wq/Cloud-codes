"""Сборка сообщений для Telegram."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from conftest import make_order
from fparser.formatter import (
    format_budget,
    format_digest,
    format_order,
    humanize_age,
    plural,
    truncate,
)

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def test_special_characters_are_escaped():
    """Заголовки с бирж содержат `<` и `&`. Без экранирования Bot API отвечает
    ошибкой разметки и сообщение просто не доходит."""
    order = make_order(title="Нужен парсер <div> & таблиц", description="A < B & C")

    text = format_order(order, now=NOW)

    assert "&lt;div&gt;" in text
    assert "&amp;" in text
    assert "<div>" not in text


def test_bold_title_and_link_are_preserved():
    order = make_order(title="Заголовок")

    assert "<b>Заголовок</b>" in format_order(order, now=NOW)


def test_message_contains_budget_category_and_source():
    order = make_order(budget_min=15000, budget_max=25000, category="Сайты", responses_count=3)

    text = format_order(order, now=NOW)

    assert "15 000 – 25 000 ₽" in text
    assert "Сайты" in text
    assert "Kwork" in text
    assert "3 отклика" in text


def test_missing_fields_are_omitted_not_broken():
    order = make_order(category=None, responses_count=None, published_at=None)

    text = format_order(order, now=NOW)

    assert "бюджет не указан" in text
    assert "None" not in text


@pytest.mark.parametrize(
    ("low", "high", "expected"),
    [
        (15000, 25000, "15 000 – 25 000 ₽"),
        (5000, None, "от 5 000 ₽"),
        (None, 30000, "до 30 000 ₽"),
        (None, None, "бюджет не указан"),
    ],
)
def test_format_budget(low, high, expected):
    assert format_budget(make_order(budget_min=low, budget_max=high)) == expected


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=10), "только что"),
        (timedelta(minutes=5), "5 мин назад"),
        (timedelta(hours=1), "1 час назад"),
        (timedelta(hours=3), "3 часа назад"),
        (timedelta(hours=7), "7 часов назад"),
        (timedelta(days=1), "1 день назад"),
        (timedelta(days=3), "3 дня назад"),
    ],
)
def test_humanize_age(delta, expected):
    assert humanize_age(NOW - delta, now=NOW) == expected


def test_humanize_age_handles_clock_skew():
    """Часы биржи могут уйти вперёд — «-2 мин назад» в канале выглядит багом."""
    assert humanize_age(NOW + timedelta(minutes=2), now=NOW) == "только что"


def test_humanize_age_of_naive_datetime():
    naive = datetime(2026, 7, 28, 11, 30)

    assert humanize_age(naive, now=NOW) == "30 мин назад"


def test_humanize_age_of_none():
    assert humanize_age(None, now=NOW) is None


@pytest.mark.parametrize(
    ("count", "expected"),
    [(1, "отклик"), (2, "отклика"), (5, "откликов"), (11, "откликов"), (21, "отклик")],
)
def test_plural(count, expected):
    assert plural(count, "отклик", "отклика", "откликов") == expected


def test_truncate_cuts_on_word_boundary():
    text = "слово " * 100

    result = truncate(text, limit=20)

    assert len(result) <= 21
    assert result.endswith("…")
    assert "слов…" not in result


def test_truncate_keeps_short_text_untouched():
    assert truncate("Коротко", limit=100) == "Коротко"


def test_digest_lists_orders_with_links():
    orders = [make_order(native_id=str(i), title=f"Заказ {i}") for i in range(12)]

    text = format_digest(orders, now=NOW)

    assert "12 новых заказов" in text
    assert text.count("<a href=") == 12


def test_digest_caps_the_list_and_says_so():
    orders = [make_order(native_id=str(i), title=f"Заказ {i}") for i in range(40)]

    text = format_digest(orders, now=NOW)

    assert "40 новых заказов" in text
    assert "и ещё 15" in text


def test_digest_escapes_titles():
    orders = [make_order(native_id=str(i), title="A & <b>") for i in range(9)]

    text = format_digest(orders, now=NOW)

    assert "A &amp; &lt;b&gt;" in text
