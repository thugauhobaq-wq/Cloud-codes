"""Арифметика заказа: доставка, порог бесплатной доставки, минимальная сумма.

Отдельный модуль без базы и Telegram: это то, что покупатель видит в итоге
и о чём будет спорить, если посчитать неправильно.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .models import DELIVERY_COURIER, Cart


@dataclass(slots=True)
class Totals:
    goods: int
    delivery: int
    total: int
    free_delivery_applied: bool = False
    #: Сколько не хватает до бесплатной доставки. 0 — порог уже пройден или не задан.
    to_free_delivery: int = 0


def totals(cart: Cart, settings: Settings, delivery: str) -> Totals:
    goods = cart.subtotal
    if delivery != DELIVERY_COURIER:
        return Totals(goods=goods, delivery=0, total=goods)

    threshold = settings.free_delivery_from
    if threshold and goods >= threshold:
        return Totals(goods=goods, delivery=0, total=goods, free_delivery_applied=True)

    price = settings.delivery_price
    return Totals(
        goods=goods,
        delivery=price,
        total=goods + price,
        to_free_delivery=max(0, threshold - goods) if threshold else 0,
    )


def below_minimum(cart: Cart, settings: Settings) -> int:
    """Сколько не хватает до минимального заказа. 0 — можно оформлять.

    Порог считаем по товарам, без доставки: иначе минимальный заказ
    «набирался» бы за счёт доставки, что покупателя только злит.
    """
    if not settings.min_order:
        return 0
    return max(0, settings.min_order - cart.subtotal)


def fmt_money(amount: int, currency: str = "₽") -> str:
    return f"{amount:,}".replace(",", " ") + f" {currency}"
