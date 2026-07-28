"""Арифметика предложения: скидки, НДС, округление."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from proposals.models import (
    VAT_ADDED,
    VAT_INCLUDED,
    VAT_NONE,
    LineItem,
    Party,
    Proposal,
)


def make(**kwargs) -> Proposal:
    base = {
        "items": [LineItem(title="Работа", quantity=2, price="1000", unit="ч")],
        "sender": Party(name="Студия"),
        "client": Party(name="Клиент"),
    }
    return Proposal(**{**base, **kwargs})


def test_line_item_accepts_numbers_and_strings():
    item = LineItem(title="X", quantity="1,5", price="1 200,50")
    assert item.quantity == Decimal("1.500")
    assert item.price == Decimal("1200.50")
    assert item.gross == Decimal("1800.75")


def test_item_discount():
    item = LineItem(title="X", quantity=300, price=140, discount_percent=10)
    assert item.gross == Decimal("42000.00")
    assert item.discount == Decimal("4200.00")
    assert item.total == Decimal("37800.00")


def test_totals_without_vat():
    totals = make().totals()
    assert totals.subtotal == Decimal("2000.00")
    assert totals.vat == Decimal("0.00")
    assert totals.total == Decimal("2000.00")


def test_totals_with_general_discount():
    totals = make(discount_percent=5).totals()
    assert totals.discount == Decimal("100.00")
    assert totals.total == Decimal("1900.00")


def test_vat_added_on_top():
    totals = make(vat_mode=VAT_ADDED, vat_rate=20).totals()
    assert totals.net == Decimal("2000.00")
    assert totals.vat == Decimal("400.00")
    assert totals.total == Decimal("2400.00")


def test_vat_included_is_extracted_not_added():
    """«В том числе» не меняет сумму к оплате — только выделяет НДС."""
    totals = make(vat_mode=VAT_INCLUDED, vat_rate=20).totals()
    assert totals.total == Decimal("2000.00")
    assert totals.vat == Decimal("333.33")


def test_discount_applies_before_vat():
    totals = make(vat_mode=VAT_ADDED, vat_rate=20, discount_percent=10).totals()
    assert totals.net == Decimal("1800.00")
    assert totals.vat == Decimal("360.00")
    assert totals.total == Decimal("2160.00")


def test_item_and_general_discounts_stack():
    proposal = make(
        items=[LineItem(title="A", quantity=1, price=1000, discount_percent=10)],
        discount_percent=10,
    )
    totals = proposal.totals()
    assert totals.item_discount == Decimal("100.00")
    assert totals.subtotal == Decimal("900.00")
    assert totals.discount == Decimal("90.00")
    assert totals.total == Decimal("810.00")


def test_totals_match_sum_of_rows():
    """Итог не должен разъезжаться с суммой строк из-за округлений."""
    items = [LineItem(title=f"№{n}", quantity=3, price="33.33") for n in range(7)]
    totals = make(items=items).totals()
    assert totals.subtotal == sum(item.total for item in items)


def test_valid_until():
    proposal = make(issued_at=date(2026, 7, 28), valid_days=14)
    assert proposal.valid_until == date(2026, 8, 11)


def test_vat_note():
    assert make().vat_note == "НДС не облагается"
    assert make(vat_mode=VAT_INCLUDED, vat_rate=20).vat_note == "В том числе НДС 20%"
    assert make(vat_mode=VAT_ADDED, vat_rate="7.5").vat_note == "НДС 7,5% сверху"


def test_roundtrip_through_dict():
    proposal = make(
        number="47",
        issued_at=date(2026, 7, 28),
        vat_mode=VAT_ADDED,
        discount_percent=5,
        subject="Тема",
    )
    restored = Proposal.from_dict(proposal.to_dict())
    assert restored.to_dict() == proposal.to_dict()
    assert restored.totals().total == proposal.totals().total


def test_from_dict_survives_garbage():
    proposal = Proposal.from_dict(
        {
            "number": " 5 ",
            "issued_at": "31.12.2026",
            "valid_days": "нет",
            "vat_mode": "магия",
            "items": [{"title": "A", "quantity": "x", "price": "1 000"}, {}],
            "currency": "rub",
        }
    )
    assert proposal.number == "5"
    assert proposal.issued_at == date(2026, 12, 31)
    assert proposal.valid_days == 14
    assert proposal.vat_mode == VAT_NONE
    assert proposal.currency == "RUB"
    # Пустая позиция отброшена, у оставшейся нечитаемое количество стало нулём.
    assert len(proposal.items) == 1
    assert proposal.items[0].quantity == Decimal("0.000")


@pytest.mark.parametrize(
    ("client", "expected"),
    [("ООО «Ромашка»", "KP-47-ooo-romashka.pdf"), ("", "KP-47.pdf")],
)
def test_filename_is_transliterated(client, expected):
    assert make(number="47", client=Party(name=client)).filename == expected


def test_validate_reports_missing_pieces():
    problems = Proposal().validate()
    assert "не указан исполнитель" in problems
    assert "в предложении нет ни одной позиции" in problems
    assert make().validate() == []
