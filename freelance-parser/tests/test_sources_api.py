"""hh.ru и Freelancehunt: разбор ответов официальных API."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from fparser.sources.base import Fetcher, FetchError
from fparser.sources.freelancehunt import FreelancehuntSource
from fparser.sources.hh import API_URL as HH_URL
from fparser.sources.hh import USER_AGENT, HhSource

MSK = timezone(timedelta(hours=3))


def payload(read_fixture, name: str) -> dict:
    return json.loads(read_fixture(name).decode())


# ── hh.ru ─────────────────────────────────────────────────────────────────────


def test_hh_parses_vacancies(read_fixture):
    parsed = HhSource().parse(payload(read_fixture, "hh.json"))

    assert [order.uid for order in parsed] == ["hh:98765432", "hh:98765433", "hh:98765434"]
    assert parsed[0].url == "https://hh.ru/vacancy/98765432"
    assert parsed[0].published_at == datetime(2026, 9, 12, 9, 15, tzinfo=MSK)


def test_hh_reads_salary_range(read_fixture):
    order = HhSource().parse(payload(read_fixture, "hh.json"))[0]

    assert (order.budget_min, order.budget_max, order.currency) == (80000, 150000, "₽")


def test_hh_survives_missing_salary(read_fixture):
    """У части вакансий зарплата скрыта — это норма, а не повод падать."""
    order = HhSource().parse(payload(read_fixture, "hh.json"))[1]

    assert (order.budget_min, order.budget_max) == (None, None)


def test_hh_keeps_foreign_currency(read_fixture):
    """Показать евро знаком рубля значит соврать о сумме."""
    order = HhSource().parse(payload(read_fixture, "hh.json"))[2]

    assert (order.budget_min, order.budget_max, order.currency) == (None, 2000, "€")


def test_hh_strips_highlight_markup(read_fixture):
    """hh подсвечивает найденные слова тегами — в сообщение они попасть не должны."""
    order = HhSource().parse(payload(read_fixture, "hh.json"))[0]

    assert "<highlighttext>" not in order.description
    assert "Опыт с aiogram от года." in order.description


def test_hh_category_falls_back_to_employer(read_fixture):
    parsed = HhSource().parse(payload(read_fixture, "hh.json"))

    assert parsed[0].category == "Программист, разработчик"
    assert parsed[2].category == "Intl Corp"


@pytest.mark.parametrize("broken", [{}, {"items": None}, {"items": [None, 42, {}]}])
def test_hh_ignores_malformed_payload(broken):
    assert HhSource().parse(broken) == []


@respx.mock
async def test_hh_introduces_itself(read_fixture):
    """hh просит приложения представляться, а не притворяться браузером.
    Проверяем заодно, что собственный заголовок источника доходит до запроса."""
    route = respx.get(HH_URL).mock(
        return_value=httpx.Response(200, content=read_fixture("hh.json"))
    )

    fetcher = Fetcher()
    await HhSource().fetch(fetcher)
    await fetcher.aclose()

    request = route.calls.last.request
    assert request.headers["User-Agent"] == USER_AGENT
    assert "telegram" in request.url.params["text"].lower()
    assert request.url.params["schedule"] == "remote"


@respx.mock
async def test_hh_query_is_configurable(read_fixture):
    route = respx.get(HH_URL).mock(
        return_value=httpx.Response(200, content=read_fixture("hh.json"))
    )

    fetcher = Fetcher()
    await HhSource(query="парсер данных").fetch(fetcher)
    await fetcher.aclose()

    assert route.calls.last.request.url.params["text"] == "парсер данных"


# ── Freelancehunt ─────────────────────────────────────────────────────────────


def test_fh_parses_projects(read_fixture):
    parsed = FreelancehuntSource(token="x").parse(payload(read_fixture, "freelancehunt.json"))

    assert [order.uid for order in parsed] == ["fh:1310239", "fh:1310240", "fh:1310241"]
    assert parsed[0].title == "Разработка Telegram-бота для записи клиентов"
    assert parsed[0].published_at == datetime(2026, 9, 12, 10, 0, tzinfo=MSK)


def test_fh_keeps_each_currency(read_fixture):
    """Гривны и доллары в одной ленте — курс не пересчитываем, показываем как есть."""
    parsed = FreelancehuntSource(token="x").parse(payload(read_fixture, "freelancehunt.json"))

    assert (parsed[0].budget_min, parsed[0].currency) == (12000, "₴")
    assert (parsed[1].budget_min, parsed[1].currency) == (300, "$")


def test_fh_handles_project_without_budget(read_fixture):
    parsed = FreelancehuntSource(token="x").parse(payload(read_fixture, "freelancehunt.json"))

    assert parsed[2].budget_min is None
    assert parsed[2].category is None


def test_fh_falls_back_to_built_link(read_fixture):
    """Без блока links ссылку собираем сами — заказ без ссылки бесполезен."""
    parsed = FreelancehuntSource(token="x").parse(payload(read_fixture, "freelancehunt.json"))

    assert parsed[0].url.endswith("/1310239.html")
    assert parsed[2].url == "https://freelancehunt.com/project/1310241.html"


def test_fh_decodes_entities(read_fixture):
    parsed = FreelancehuntSource(token="x").parse(payload(read_fixture, "freelancehunt.json"))

    assert "Сбор данных & выгрузка" in parsed[1].description


@pytest.mark.parametrize("broken", [{}, {"data": None}, {"data": [None, {"id": 1}]}])
def test_fh_ignores_malformed_payload(broken):
    assert FreelancehuntSource(token="x").parse(broken) == []


async def test_fh_without_token_explains_what_to_do():
    """Пустой токен — частый случай, и он обязан читаться как инструкция,
    а не как трейсбек."""
    with pytest.raises(FetchError) as exc_info:
        await FreelancehuntSource(token="").fetch(Fetcher())

    message = str(exc_info.value)
    assert "FREELANCEHUNT_TOKEN" in message
    assert "/sources" in message


@respx.mock
async def test_fh_sends_bearer_token(read_fixture):
    route = respx.get("https://api.freelancehunt.com/v2/projects").mock(
        return_value=httpx.Response(200, content=read_fixture("freelancehunt.json"))
    )

    fetcher = Fetcher()
    await FreelancehuntSource(token="fh_abc123").fetch(fetcher)
    await fetcher.aclose()

    assert route.calls.last.request.headers["Authorization"] == "Bearer fh_abc123"


async def test_fh_rejects_non_ascii_token_readably():
    """Ключ, скопированный с лишними символами, должен объяснять себя, а не
    падать UnicodeEncodeError из недр HTTP-клиента."""
    with pytest.raises(FetchError) as exc_info:
        await FreelancehuntSource(token="ключ").fetch(Fetcher())

    assert "нелатинские" in str(exc_info.value)
