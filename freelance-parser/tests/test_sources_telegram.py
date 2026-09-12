"""Telegram-каналы: разбор страницы t.me/s/ и поведение при сбоях."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from fparser.sources.base import Fetcher, FetchError
from fparser.sources.telegram import (
    MIN_POST_LENGTH,
    TelegramChannelsSource,
    channel_url,
    normalize_channel,
    parse_channel,
)


def orders(read_fixture, channel: str = "botorders"):
    return parse_channel(read_fixture("tg_channel.html").decode(), channel)


# ── разбор страницы ───────────────────────────────────────────────────────────


def test_parses_posts_with_stable_ids(read_fixture):
    """`data-post` уже содержит «канал/номер» — готовый ключ дедупликации."""
    assert [order.uid for order in orders(read_fixture)] == [
        "tg:botorders/1041",
        "tg:botorders/1042",
        "tg:botorders/1044",
    ]


def test_skips_short_and_textless_posts(read_fixture):
    """Приветствия и подписи к картинкам — не заказы, и в ленте они шум."""
    titles = [order.title for order in orders(read_fixture)]

    assert "Всем привет!" not in titles
    assert all(len(order.title) >= 10 for order in orders(read_fixture))


def test_first_line_becomes_title_rest_description(read_fixture):
    order = orders(read_fixture)[0]

    assert order.title == "Нужен Telegram-бот для записи в барбершоп"
    assert order.description.startswith("Запись на услугу")
    assert "Бюджет 30 000 руб." in order.description


def test_line_breaks_survive(read_fixture):
    """В постах переносы сделаны через `<br>`; без них абзацы слипаются."""
    order = orders(read_fixture)[0]

    assert "\n" in order.description
    assert "мастера.Бюджет" not in order.description


def test_budget_is_read_from_post_text(read_fixture):
    parsed = orders(read_fixture)

    assert (parsed[0].budget_min, parsed[0].budget_max) == (30000, None)
    assert (parsed[1].budget_min, parsed[1].budget_max) == (None, None)
    assert (parsed[2].budget_min, parsed[2].budget_max) == (15000, 40000)


def test_long_first_line_is_truncated(read_fixture):
    order = orders(read_fixture)[2]

    assert order.title.endswith("…")
    assert len(order.title) <= 121


def test_channel_becomes_category(read_fixture):
    """Канал кладём в рубрику — тогда включать и выключать его можно уже
    существующей командой /categories, не дописывая ничего."""
    assert {order.category for order in orders(read_fixture)} == {"@botorders"}


def test_date_and_link(read_fixture):
    order = orders(read_fixture)[0]

    assert order.published_at == datetime(2026, 9, 12, 8, 15, tzinfo=UTC)
    assert order.url == "https://t.me/botorders/1041"


def test_entities_are_decoded(read_fixture):
    assert "aiogram & PostgreSQL" in orders(read_fixture)[2].description


def test_broken_page_returns_empty():
    assert parse_channel("", "botorders") == []


def test_page_without_posts_returns_empty():
    assert parse_channel("<html><body><p>ничего</p></body></html>", "botorders") == []


def test_minimum_length_is_a_real_threshold():
    page = f'''<div class="tgme_widget_message" data-post="c/1">
      <div class="tgme_widget_message_text">{"x" * (MIN_POST_LENGTH - 1)}</div></div>'''

    assert parse_channel(page, "c") == []


# ── имена каналов ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        "@freelansim_ru",
        "freelansim_ru",
        "https://t.me/freelansim_ru",
        "t.me/s/freelansim_ru",
        "https://t.me/s/freelansim_ru/123",
    ],
)
def test_channel_name_is_understood_in_any_form(raw):
    """Пользователь чаще копирует ссылку, чем печатает имя. Разбирать это
    должен код, а не человек."""
    assert normalize_channel(raw) == "freelansim_ru"


@pytest.mark.parametrize("raw", ["", "   ", "@ab", "не канал", "@плохое-имя", "123start"])
def test_bad_channel_names_are_rejected(raw):
    assert normalize_channel(raw) is None


# ── поведение источника ───────────────────────────────────────────────────────


async def test_without_channels_says_what_to_do():
    source = TelegramChannelsSource(channels_provider=_provider([]))

    with pytest.raises(FetchError) as exc_info:
        await source.fetch(Fetcher())

    assert "/tg_add" in str(exc_info.value)


@respx.mock
async def test_one_dead_channel_does_not_hide_the_others(read_fixture):
    """Каналов в списке обычно десяток — один закрытый не повод остаться без
    остальных."""
    respx.get(channel_url("dead")).mock(return_value=httpx.Response(404))
    respx.get(channel_url("botorders")).mock(
        return_value=httpx.Response(200, content=read_fixture("tg_channel.html"))
    )
    source = TelegramChannelsSource(channels_provider=_provider(["dead", "botorders"]))

    fetched = await source.fetch(Fetcher(max_attempts=1))

    assert len(fetched) == 3
    assert {order.category for order in fetched} == {"@botorders"}


@respx.mock
async def test_all_channels_failing_raises_so_poller_backs_off():
    """Если молчат все, это надо отдать наверх: поллеру нужен сигнал уйти
    в паузу, а не вывод «заказов нет»."""
    respx.get(channel_url("a")).mock(return_value=httpx.Response(403))
    respx.get(channel_url("b")).mock(return_value=httpx.Response(403))
    source = TelegramChannelsSource(channels_provider=_provider(["a", "b"]))

    with pytest.raises(FetchError) as exc_info:
        await source.fetch(Fetcher(max_attempts=1))

    assert exc_info.value.is_blocked


def _provider(channels: list[str]):
    async def provide() -> list[str]:
        return channels

    return provide
