from __future__ import annotations

import pytest

from shop.config import Settings
from shop.models import PAY_CASH, PAY_ONLINE, PAYMENT_PAID, STATUS_CANCELLED, Order, OrderLine
from shop.payments import (
    DESCRIPTION_LIMIT,
    invoice_description,
    invoice_lines,
    invoice_total,
    order_payload,
    parse_payload,
    precheckout_problem,
    to_minor,
)


def make_order(**kwargs) -> Order:
    kwargs.setdefault("id", 7)
    kwargs.setdefault("customer_id", 42)
    kwargs.setdefault(
        "lines", [OrderLine(product_id=1, title="Чехол", price=990, qty=2)]
    )
    kwargs.setdefault("goods_total", 1980)
    kwargs.setdefault("payment_method", PAY_ONLINE)
    return Order(**kwargs)


# ── суммы ─────────────────────────────────────────────────────────────────


def test_amounts_go_to_telegram_in_kopecks():
    """Telegram считает в минимальных единицах: рубли надо умножать на 100."""
    assert to_minor(990) == 99000


def test_invoice_lines_repeat_the_order():
    order = make_order()
    assert invoice_lines(order) == [("Чехол ×2", 198000)]
    assert invoice_total(order) == to_minor(order.total)


def test_single_item_has_no_multiplier_in_the_label():
    order = make_order(lines=[OrderLine(product_id=1, title="Чехол", price=990, qty=1)])
    assert invoice_lines(order)[0][0] == "Чехол"


def test_paid_delivery_is_a_separate_line():
    order = make_order(delivery_price=300)
    labels = [label for label, _ in invoice_lines(order)]

    assert "Доставка" in labels
    assert invoice_total(order) == to_minor(1980 + 300)


def test_free_delivery_adds_no_line():
    order = make_order(delivery_price=0)
    assert [label for label, _ in invoice_lines(order)] == ["Чехол ×2"]


def test_invoice_total_always_matches_the_order():
    """Сумма позиций счёта обязана совпадать с суммой заказа до копейки."""
    order = make_order(
        lines=[
            OrderLine(product_id=1, title="Чехол", price=990, qty=2),
            OrderLine(product_id=2, title="Кружка", price=690, qty=3),
        ],
        goods_total=1980 + 2070,
        delivery_price=300,
    )
    assert invoice_total(order) == to_minor(order.total)


# ── описание ──────────────────────────────────────────────────────────────


def test_description_lists_the_goods(settings: Settings):
    order = make_order()
    assert invoice_description(order, settings) == "Чехол ×2"


def test_long_description_is_trimmed_to_the_telegram_limit(settings: Settings):
    order = make_order(
        lines=[
            OrderLine(product_id=i, title=f"Товар {i} с длинным названием", price=100, qty=1)
            for i in range(20)
        ]
    )
    description = invoice_description(order, settings)

    assert len(description) <= DESCRIPTION_LIMIT
    assert description.endswith("…")


def test_description_falls_back_to_the_shop_name(settings: Settings):
    assert invoice_description(make_order(lines=[]), settings) == settings.shop_name


# ── payload ───────────────────────────────────────────────────────────────


def test_payload_roundtrip():
    assert parse_payload(order_payload(12)) == 12


@pytest.mark.parametrize("payload", ["", None, "order:", "order:abc", "cart:12", "12"])
def test_foreign_payload_is_not_ours(payload):
    """Платёж с чужим payload не должен превратиться в оплату случайного заказа."""
    assert parse_payload(payload) is None


# ── проверка перед списанием ──────────────────────────────────────────────


def test_valid_order_passes(settings: Settings):
    order = make_order()
    assert precheckout_problem(order, invoice_total(order), settings) is None


def test_missing_order_is_refused(settings: Settings):
    assert precheckout_problem(None, 198000, settings) is not None


def test_already_paid_order_is_refused(settings: Settings):
    """Второе списание за тот же заказ пришлось бы возвращать руками."""
    order = make_order(payment_status=PAYMENT_PAID)
    problem = precheckout_problem(order, invoice_total(order), settings)
    assert problem is not None
    assert "оплачен" in problem


def test_cancelled_order_is_refused(settings: Settings):
    order = make_order(status=STATUS_CANCELLED)
    assert precheckout_problem(order, invoice_total(order), settings) is not None


def test_order_switched_to_cash_is_refused(settings: Settings):
    order = make_order(payment_method=PAY_CASH)
    assert precheckout_problem(order, invoice_total(order), settings) is not None


def test_changed_amount_is_refused(settings: Settings):
    """Счёт устарел: списать не ту сумму, о которой договорились, нельзя."""
    order = make_order()
    problem = precheckout_problem(order, invoice_total(order) - 100, settings)
    assert problem is not None
    assert "изменилась" in problem


# ── настройки ─────────────────────────────────────────────────────────────


def test_online_payment_needs_a_provider_token(settings: Settings):
    assert not settings.online_payment_enabled
    assert settings.payment_choices() == ["cash"]

    settings.payment_provider_token = "1234:TEST:token"
    assert settings.online_payment_enabled
    assert settings.payment_choices() == ["online", "cash"]


def test_shop_without_any_payment_choice(settings: Settings):
    settings.cash_payment_enabled = False
    assert settings.payment_choices() == []
