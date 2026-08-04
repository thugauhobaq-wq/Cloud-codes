"""Логика отбора заказов."""

from __future__ import annotations

from conftest import make_order
from fparser.filters import apply, contains_word, match, normalize
from fparser.storage import Filters


def test_no_keywords_lets_everything_through():
    order = make_order(title="Что угодно")

    assert match(order, Filters()).passed


def test_keyword_matches_word_form():
    order = make_order(title="Нужен Telegram-бота срочно")

    assert match(order, Filters(keywords=["бот"])).passed


def test_keyword_does_not_match_inside_another_word():
    """«бот» встречается внутри «работа» — при поиске подстрокой канал завалило
    бы мусором, поэтому совпадение привязано к началу слова."""
    order = make_order(title="Работа с текстами")

    assert not match(order, Filters(keywords=["бот"])).passed


def test_yo_is_equivalent_to_ye():
    """На биржах пишут и «вёрстка», и «верстка» — фильтр обязан ловить оба."""
    order = make_order(title="Требуется вёрстка лендинга")

    assert match(order, Filters(keywords=["верстка"])).passed
    assert contains_word("верстка сайта", "вёрстка")


def test_keyword_matches_description_too():
    order = make_order(title="Задача", description="Стек: Python и aiogram")

    assert match(order, Filters(keywords=["python"])).passed


def test_stopword_rejects_order():
    order = make_order(title="Нужен логотип для кофейни")
    result = match(order, Filters(stopwords=["логотип"]))

    assert not result.passed
    assert "логотип" in result.reason


def test_stopword_wins_over_keyword():
    """Стоп-слово — способ выключить тему целиком, поэтому оно сильнее."""
    order = make_order(title="Логотип и лендинг")

    assert not match(order, Filters(keywords=["лендинг"], stopwords=["логотип"])).passed


def test_min_budget_rejects_cheap_orders():
    order = make_order(budget_min=3000)

    assert not match(order, Filters(min_budget=5000)).passed
    assert match(order, Filters(min_budget=3000)).passed


def test_unknown_budget_passes_by_default():
    """RSS часто не отдаёт сумму. Отбрасывать всё безбюджетное по умолчанию
    значило бы выкосить большую часть ленты."""
    order = make_order(budget_min=None, budget_max=None)

    assert match(order, Filters(min_budget=10000)).passed


def test_unknown_budget_can_be_rejected_explicitly():
    order = make_order(budget_min=None, budget_max=None)
    filters = Filters(min_budget=10000, skip_unknown_budget=True)

    assert not match(order, filters).passed


def test_skip_unknown_budget_needs_a_threshold():
    """Без порога отбрасывать «бюджет не указан» бессмысленно."""
    order = make_order(budget_min=None)

    assert match(order, Filters(min_budget=0, skip_unknown_budget=True)).passed


def test_range_is_judged_by_lower_bound():
    order = make_order(budget_min=3000, budget_max=90000)

    assert not match(order, Filters(min_budget=5000)).passed


def test_disabled_source_is_rejected():
    order = make_order(source="kwork")

    assert not match(order, Filters(sources=["flru"])).passed


def test_category_whitelist():
    order = make_order(category="Разработка сайтов")

    assert match(order, Filters(categories=["разработка сайтов"])).passed
    assert not match(order, Filters(categories=["Дизайн"])).passed


def test_order_without_category_is_rejected_by_whitelist():
    order = make_order(category=None)

    assert not match(order, Filters(categories=["Дизайн"])).passed


def test_apply_keeps_only_matching():
    orders = [
        make_order(native_id="1", title="Нужен лендинг"),
        make_order(native_id="2", title="Нужен логотип"),
    ]

    kept = apply(orders, Filters(keywords=["лендинг"]))

    assert [order.native_id for order in kept] == ["1"]


def test_normalize():
    assert normalize("Вёрстка ЛЕНДИНГА") == "верстка лендинга"


def test_empty_keyword_never_matches():
    assert not contains_word("любой текст", "   ")
