"""Kwork: основной путь через JSON-стейт и запасной через вёрстку."""

from __future__ import annotations

from datetime import UTC, datetime

from fparser.sources.kwork import KworkSource, extract_state_projects, parse_cards


def test_parses_orders_from_state(read_fixture):
    orders = KworkSource().parse(read_fixture("kwork_state.html").decode())

    assert [order.uid for order in orders] == [
        "kwork:991001",
        "kwork:991002",
        "kwork:991003",
    ]


def test_state_json_survives_braces_and_quotes_inside_strings(read_fixture):
    """Стейт вырезается подсчётом скобок, а не регуляркой: в описаниях
    попадаются и `}`, и экранированные кавычки."""
    orders = KworkSource().parse(read_fixture("kwork_state.html").decode())

    assert orders[0].title == "Нужен лендинг на Tilda {срочно}"
    assert '"кавычка"' in orders[0].description
    assert orders[0].description.endswith("скобка }.")


def test_maps_all_fields_from_state(read_fixture):
    order = KworkSource().parse(read_fixture("kwork_state.html").decode())[0]

    assert (order.budget_min, order.budget_max) == (15000, 25000)
    assert order.category == "Разработка сайтов"
    assert order.responses_count == 3
    assert order.url == "https://kwork.ru/projects/991001"
    assert order.published_at == datetime(2026, 7, 28, 4, 10, tzinfo=UTC)


def test_handles_alternative_key_names(read_fixture):
    """Kwork не раз переименовывал поля, поэтому проверяем и `desc`,
    и `offers_count`, и голый `priceLimit` без нижней границы."""
    order = KworkSource().parse(read_fixture("kwork_state.html").decode())[1]

    assert order.description == "Нужен бот на Python с уведомлениями"
    assert order.responses_count == 7
    assert (order.budget_min, order.budget_max) == (None, 30000)


def test_unix_timestamp_is_parsed(read_fixture):
    order = KworkSource().parse(read_fixture("kwork_state.html").decode())[2]

    assert order.published_at == datetime(2026, 7, 28, 4, 14, tzinfo=UTC)


def test_ignores_non_project_objects(read_fixture):
    """В стейте рядом лежат пользователь и категории — у них тоже есть id и
    name, и без фильтра по форме они уехали бы в канал как заказы."""
    projects = extract_state_projects(read_fixture("kwork_state.html").decode())

    names = {project.get("name") for project in projects}
    assert "Тестовый Пользователь" not in names
    assert "Разработка" not in names
    assert len(projects) == 3


def test_falls_back_to_cards_when_state_missing(read_fixture):
    orders = KworkSource().parse(read_fixture("kwork_cards.html").decode())

    assert [order.uid for order in orders] == [
        "kwork:880101",
        "kwork:880102",
        "kwork:880103",
    ]


def test_card_budget_does_not_leak_between_cards(read_fixture):
    """Подъём по дереву обязан остановиться на своей карточке — иначе заказу
    припишется цена соседа, и ошибка будет выглядеть правдоподобно."""
    orders = parse_cards(read_fixture("kwork_cards.html").decode())

    budgets = [(order.budget_min, order.budget_max) for order in orders]
    assert budgets == [(None, 120000), (8000, None), (5000, 12000)]


def test_cards_skip_navigation_links(read_fixture):
    orders = parse_cards(read_fixture("kwork_cards.html").decode())

    assert all(order.title != "Проекты" for order in orders)
    assert len(orders) == 3


def test_broken_html_returns_empty_instead_of_raising():
    assert KworkSource().parse("") == []


def test_state_without_projects_falls_through():
    page = '<html><script>window.stateData = {"page":{"empty":true}};</script></html>'

    assert KworkSource().parse(page) == []
