"""Разбор RSS/Atom-лент трёх площадок."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fparser.sources.base import extract_budget, id_from_url, parse_date, parse_feed, strip_html
from fparser.sources.flru import FlRuSource
from fparser.sources.habr import HabrSource
from fparser.sources.weblancer import WeblancerSource


def orders_from(source, raw: bytes):
    return [order for item in parse_feed(raw) if (order := source.to_order(item))]


# ── FL.ru ─────────────────────────────────────────────────────────────────────


def test_flru_parses_all_items(read_fixture):
    orders = orders_from(FlRuSource(), read_fixture("flru.xml"))
    assert [order.uid for order in orders] == [
        "flru:5461234",
        "flru:5461235",
        "flru:5461236",
    ]


def test_flru_strips_budget_from_title(read_fixture):
    orders = orders_from(FlRuSource(), read_fixture("flru.xml"))

    # Бюджет показывается отдельной строкой, дублировать его в заголовке незачем.
    assert orders[0].title == "Вёрстка лендинга по макету"
    assert orders[1].title == "Telegram-бот на Python"


def test_flru_extracts_budget_range(read_fixture):
    orders = orders_from(FlRuSource(), read_fixture("flru.xml"))

    assert (orders[0].budget_min, orders[0].budget_max) == (5000, None)
    assert (orders[1].budget_min, orders[1].budget_max) == (15000, 30000)
    assert (orders[2].budget_min, orders[2].budget_max) == (None, None)


def test_flru_keeps_category_and_date(read_fixture):
    order = orders_from(FlRuSource(), read_fixture("flru.xml"))[0]

    assert order.category == "Вёрстка сайтов"
    assert order.published_at == datetime(2026, 7, 28, 1, 0, tzinfo=UTC)


def test_flru_decodes_html_entities(read_fixture):
    orders = orders_from(FlRuSource(), read_fixture("flru.xml"))

    assert "&amp;" not in orders[1].description
    assert "aiogram & PostgreSQL" in orders[1].description


def test_flru_keeps_sentence_boundary_from_br(read_fixture):
    """`<br/>` обязан превратиться в перенос, иначе предложения слипнутся."""
    order = orders_from(FlRuSource(), read_fixture("flru.xml"))[0]

    assert "Figma.Адаптив" not in order.description
    assert "Адаптив обязателен" in order.description


# ── Habr Freelance (Atom) ─────────────────────────────────────────────────────


def test_habr_parses_atom(read_fixture):
    orders = orders_from(HabrSource(), read_fixture("habr.xml"))

    assert [order.uid for order in orders] == ["habr:812345", "habr:812346"]
    assert orders[0].url == "https://freelance.habr.com/tasks/812345"


def test_habr_reads_category_from_term_attribute(read_fixture):
    """В Atom рубрика лежит в атрибуте `term`, а не в тексте тега."""
    orders = orders_from(HabrSource(), read_fixture("habr.xml"))

    assert orders[0].category == "Веб-разработка"
    assert orders[1].category == "DevOps"


def test_habr_parses_iso_date(read_fixture):
    order = orders_from(HabrSource(), read_fixture("habr.xml"))[0]

    assert order.published_at == datetime(2026, 7, 28, 1, 10, tzinfo=UTC)


# ── Weblancer ─────────────────────────────────────────────────────────────────


def test_weblancer_takes_id_from_url_tail(read_fixture):
    orders = orders_from(WeblancerSource(), read_fixture("weblancer.xml"))

    assert [order.uid for order in orders] == ["weblancer:1234567", "weblancer:1234568"]
    assert orders[0].budget_min == 60000


# ── общие помощники ───────────────────────────────────────────────────────────


def test_parse_feed_survives_broken_xml():
    """Ленты бирж содержат неэкранированные амперсанды — весь опрос из-за
    одной битой записи падать не должен."""
    broken = """<?xml version="1.0"?><rss><channel>
    <item><title>A & B</title><link>https://x.ru/projects/1/</link></item>
    <item><title>Второй</title><link>https://x.ru/projects/2/</link></item>
    </channel></rss>""".encode()

    items = parse_feed(broken)

    assert len(items) >= 1
    assert any(item.link.endswith("/2/") for item in items)


def test_parse_feed_skips_items_without_link():
    raw = """<?xml version="1.0"?><rss><channel>
    <item><title>Без ссылки</title></item>
    <item><title>Нормальный</title><link>https://x.ru/projects/7/</link></item>
    </channel></rss>""".encode()

    items = parse_feed(raw)

    assert [item.title for item in items] == ["Нормальный"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("5000 руб.", (5000, None)),
        ("Бюджет: 5 000 руб.", (5000, None)),
        ("от 15000 до 30 000 руб.", (15000, 30000)),
        ("5000 - 12000 руб.", (5000, 12000)),
        ("15000—30000 ₽", (15000, 30000)),
        ("бюджет обсуждается", (None, None)),
        ("", (None, None)),
    ],
)
def test_extract_budget(text, expected):
    assert extract_budget(text) == expected


def test_upper_bound_only_is_not_treated_as_minimum():
    """«до 120 000» — потолок. Записав его в минимум, мы пропускали бы такие
    заказы через фильтр «от 100 000», хотя заплатят по ним может и тысячу."""
    assert extract_budget("до 120 000 руб.") == (None, 120000)


def test_extract_budget_swaps_reversed_range():
    assert extract_budget("от 30000 до 15000 руб.") == (15000, 30000)


def test_id_from_url_is_stable_without_digits():
    """Без числового id ключ дедупликации всё равно обязан быть постоянным."""
    url = "https://example.com/projects/slug-without-numbers/"

    assert id_from_url(url) == id_from_url(url)


def test_parse_date_handles_both_formats():
    assert parse_date("Tue, 28 Jul 2026 04:00:00 +0300") == datetime(
        2026, 7, 28, 1, 0, tzinfo=UTC
    )
    assert parse_date("2026-07-28T01:10:00Z") == datetime(2026, 7, 28, 1, 10, tzinfo=UTC)
    assert parse_date("не дата") is None
    assert parse_date(None) is None


def test_strip_html_collapses_whitespace():
    assert strip_html("<p>Привет&nbsp;&amp; пока</p>") == "Привет & пока"
