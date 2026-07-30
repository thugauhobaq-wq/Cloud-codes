"""Деньги, проценты и сумма прописью."""

from __future__ import annotations

from decimal import Decimal

import pytest

from proposals.money import (
    amount_in_words,
    format_amount,
    format_quantity,
    money,
    number_to_words,
    plural,
    quantity,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1234.5", "1234.50"),
        ("1 234,50", "1234.50"),
        ("1 234,50 ₽", "1234.50"),
        ("1.234.567,89", "1234567.89"),
        (1999, "1999.00"),
        (0.1 + 0.2, "0.30"),
        ("", "0.00"),
        ("не число", "0.00"),
        ("-500", "-500.00"),
    ],
)
def test_money_parses_human_input(raw, expected):
    assert money(raw) == Decimal(expected)


def test_money_rounds_half_up():
    # Банковское округление Python отправило бы 2.675 в 2.67.
    assert money(Decimal("2.675")) == Decimal("2.68")
    assert money(Decimal("2.665")) == Decimal("2.67")


def test_quantity_keeps_three_decimals():
    assert quantity("1,5") == Decimal("1.500")
    assert quantity("0.25") == Decimal("0.250")
    assert format_quantity(Decimal("2.000")) == "2"
    assert format_quantity(Decimal("1.5")) == "1,5"


def test_format_amount_groups_thousands():
    text = format_amount(Decimal("1234567.05"), "RUB")
    assert text.replace(" ", " ") == "1 234 567,05 ₽"
    assert format_amount(Decimal("120"), "RUB", with_symbol=False).strip() == "120,00"


def test_format_amount_uses_non_breaking_spaces():
    """Сумма не должна разрываться переносом строки посреди разрядов."""
    assert " " in format_amount(Decimal("10000"), "RUB")


@pytest.mark.parametrize(
    ("count", "expected"),
    [(1, "рубль"), (2, "рубля"), (4, "рубля"), (5, "рублей"), (11, "рублей"),
     (14, "рублей"), (21, "рубль"), (102, "рубля"), (111, "рублей")],
)
def test_plural(count, expected):
    assert plural(count, "рубль", "рубля", "рублей") == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "ноль"),
        (7, "семь"),
        (13, "тринадцать"),
        (21, "двадцать один"),
        (100, "сто"),
        (1024, "одна тысяча двадцать четыре"),
        (2000, "две тысячи"),
        (1000000, "один миллион"),
        (318060, "триста восемнадцать тысяч шестьдесят"),
    ],
)
def test_number_to_words(value, expected):
    assert number_to_words(value) == expected


def test_number_to_words_uses_feminine_for_thousands():
    """«Одна тысяча», а не «один тысяча» — у разряда свой род."""
    assert number_to_words(1000) == "одна тысяча"
    assert number_to_words(2000) == "две тысячи"


def test_amount_in_words():
    assert amount_in_words(Decimal("318060.00")) == (
        "Триста восемнадцать тысяч шестьдесят рублей 00 копеек"
    )
    assert amount_in_words(Decimal("1.01")) == "Один рубль 01 копейка"
    assert amount_in_words(Decimal("0")) == "Ноль рублей 00 копеек"


def test_amount_in_words_feminine_currency():
    assert amount_in_words(Decimal("2.00"), "UAH").startswith("Две гривны")


def test_amount_in_words_unknown_currency_keeps_code():
    assert "XXX" in amount_in_words(Decimal("5.00"), "XXX")
