from __future__ import annotations

import pytest
from botkit.publish import MODE_BRANCH, MODE_REPO

from botfactory.orders import Order, OrderError, parse


def test_latin_package_is_taken_as_is():
    order = parse("сделай бота shop")

    assert order.package == "shop"
    assert order.title == "Shop"


def test_kind_is_recognised_by_a_russian_word():
    """«для магазина» — это shop, а не magazina."""
    assert parse("сделай бота для магазина").package == "shop"
    assert parse("нужен бот для записи клиентов").package == "booking"
    assert parse("бот для отзывов").package == "reviews"


def test_explicit_latin_name_wins_over_the_dictionary():
    order = parse("сделай бота shop для магазина")

    assert order.package == "shop"


def test_unknown_russian_word_is_transliterated():
    order = parse("сделай бота теплица")

    assert order.package == "teplica"
    assert order.title == "Теплица"


def test_title_in_quotes():
    order = parse("сделай бота для магазина «Магазин у дома»")

    assert order.title == "Магазин у дома"
    assert order.package == "shop"


def test_title_after_the_word():
    order = parse("сделай бота shop, название Магазин у дома, публичный")

    assert order.title == "Магазин у дома"
    assert order.private is False


@pytest.mark.parametrize(
    ("text", "private"),
    [
        ("бота shop публичный", False),
        ("бота shop, открытый репозиторий", False),
        ("бота shop приватный", True),
        ("бота shop", True),
    ],
)
def test_visibility_is_understood_by_words(text: str, private: bool):
    assert parse(text, private=True).private is private


@pytest.mark.parametrize(
    ("text", "mode"),
    [
        ("бота shop веткой", MODE_BRANCH),
        ("бота shop в этот репозиторий", MODE_BRANCH),
        ("бота shop отдельным репозиторием", MODE_REPO),
        ("бота shop", MODE_REPO),
    ],
)
def test_publication_mode_is_understood_by_words(text: str, mode: str):
    assert parse(text, mode=MODE_REPO).mode == mode


def test_settings_give_the_defaults():
    order = parse("сделай бота shop", mode=MODE_BRANCH, private=False)

    assert order.mode == MODE_BRANCH
    assert order.private is False


def test_repo_name_is_taken_from_the_request():
    order = parse("сделай бота shop, репозиторий my-shop-bot")

    assert order.repo == "my-shop-bot"


def test_repo_word_without_a_name_is_not_a_name():
    """«в новый репозиторий» — это режим, а не имя репозитория."""
    assert parse("сделай бота shop в новый репозиторий").repo == ""


def test_command_prefix_is_ignored():
    assert parse("/new бота для магазина").package == "shop"


@pytest.mark.parametrize("text", ["", "   ", "привет", "сделай бота"])
def test_unclear_request_is_reported(text: str):
    with pytest.raises(OrderError):
        parse(text)


def test_buttons_toggle_the_order():
    order = Order(package="shop", title="Магазин", mode=MODE_REPO, private=True)

    assert order.toggled_mode().mode == MODE_BRANCH
    assert order.toggled_private().private is False
    # Заказ неизменяемый: карточка в чате не должна меняться задним числом.
    assert order.mode == MODE_REPO and order.private is True
