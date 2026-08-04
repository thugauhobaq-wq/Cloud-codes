from __future__ import annotations

from conftest import cart, product
from shop.config import Settings
from shop.models import DELIVERY_COURIER, DELIVERY_PICKUP, Cart
from shop.pricing import below_minimum, fmt_money, totals


def test_pickup_has_no_delivery_cost(settings: Settings):
    amounts = totals(cart((product(price=1000), 2)), settings, DELIVERY_PICKUP)
    assert amounts.goods == 2000
    assert amounts.delivery == 0
    assert amounts.total == 2000


def test_courier_adds_delivery_below_the_threshold(settings: Settings):
    amounts = totals(cart((product(price=1000), 1)), settings, DELIVERY_COURIER)
    assert amounts.delivery == 300
    assert amounts.total == 1300
    assert amounts.to_free_delivery == 2000


def test_free_delivery_from_the_threshold(settings: Settings):
    amounts = totals(cart((product(price=1500), 2)), settings, DELIVERY_COURIER)
    assert amounts.free_delivery_applied
    assert amounts.delivery == 0
    assert amounts.total == 3000


def test_threshold_is_inclusive(settings: Settings):
    """Ровно 3000 — уже бесплатно: «от 3000» покупатель читает именно так."""
    exact = totals(cart((product(price=3000), 1)), settings, DELIVERY_COURIER)
    almost = totals(cart((product(price=2999), 1)), settings, DELIVERY_COURIER)
    assert exact.delivery == 0
    assert almost.delivery == 300


def test_disabled_threshold_always_charges_delivery(settings: Settings):
    settings.free_delivery_from = 0
    amounts = totals(cart((product(price=100000), 1)), settings, DELIVERY_COURIER)
    assert amounts.delivery == 300
    assert amounts.to_free_delivery == 0


def test_minimum_order_counts_goods_only(settings: Settings):
    # Порог 500: доставка не должна «дотягивать» корзину до минимума.
    assert below_minimum(cart((product(price=300), 1)), settings) == 200
    assert below_minimum(cart((product(price=500), 1)), settings) == 0


def test_no_minimum_when_not_configured(settings: Settings):
    settings.min_order = 0
    assert below_minimum(Cart(), settings) == 0


def test_empty_cart_totals_to_zero(settings: Settings):
    amounts = totals(Cart(), settings, DELIVERY_PICKUP)
    assert amounts.total == 0


def test_money_formatting():
    assert fmt_money(1500) == "1 500 ₽"
    assert fmt_money(1234567, "€") == "1 234 567 €"
    assert fmt_money(0) == "0 ₽"
