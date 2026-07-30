"""Сущности магазина: категория, товар, корзина, заказ.

Цены — целые числа в рублях. Копейки маленькому продавцу не нужны, а float в
деньгах рано или поздно даёт «1 499.9999999».
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

STATUS_NEW = "new"
STATUS_ACCEPTED = "accepted"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"

STATUS_TITLES = {
    STATUS_NEW: "🆕 новый",
    STATUS_ACCEPTED: "✅ принят",
    STATUS_DONE: "📦 выполнен",
    STATUS_CANCELLED: "❌ отменён",
}

DELIVERY_PICKUP = "pickup"
DELIVERY_COURIER = "courier"

DELIVERY_TITLES = {
    DELIVERY_PICKUP: "самовывоз",
    DELIVERY_COURIER: "доставка",
}


@dataclass(slots=True)
class Category:
    id: int
    title: str
    position: int = 0
    active: bool = True


@dataclass(slots=True)
class Product:
    id: int
    title: str
    price: int
    category_id: int | None = None
    description: str = ""
    photo_id: str = ""
    #: None — товар без учёта остатков (например, услуга или печать под заказ).
    stock: int | None = None
    active: bool = True
    position: int = 0
    category_title: str = ""

    def available(self, *, track_stock: bool = True) -> bool:
        if not self.active:
            return False
        return not (track_stock and self.stock is not None and self.stock <= 0)

    def max_qty(self, *, track_stock: bool = True, hard_limit: int = 99) -> int:
        if track_stock and self.stock is not None:
            return max(0, min(self.stock, hard_limit))
        return hard_limit


@dataclass(slots=True)
class CartLine:
    """Позиция корзины: товар и количество.

    Товар держим целиком, а не по id: сумма, доступность и подпись кнопки
    нужны в одном месте, и лишний поход в базу за каждой позицией ни к чему.
    """

    product: Product
    qty: int

    @property
    def total(self) -> int:
        return self.product.price * self.qty


@dataclass(slots=True)
class Cart:
    lines: list[CartLine] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.lines)

    @property
    def subtotal(self) -> int:
        return sum(line.total for line in self.lines)

    @property
    def count(self) -> int:
        return sum(line.qty for line in self.lines)

    def line(self, product_id: int) -> CartLine | None:
        return next((line for line in self.lines if line.product.id == product_id), None)


@dataclass(slots=True)
class OrderLine:
    """Позиция заказа.

    Название и цена скопированы на момент покупки: товар потом подорожает или
    будет переименован, а в заказе должно остаться то, что видел покупатель.
    """

    product_id: int | None
    title: str
    price: int
    qty: int

    @property
    def total(self) -> int:
        return self.price * self.qty


@dataclass(slots=True)
class Order:
    id: int
    customer_id: int
    lines: list[OrderLine] = field(default_factory=list)
    goods_total: int = 0
    delivery_price: int = 0
    delivery: str = DELIVERY_PICKUP
    address: str = ""
    name: str = ""
    phone: str = ""
    comment: str = ""
    status: str = STATUS_NEW
    created_at: datetime | None = None
    username: str | None = None

    @property
    def total(self) -> int:
        return self.goods_total + self.delivery_price

    @property
    def is_open(self) -> bool:
        return self.status in {STATUS_NEW, STATUS_ACCEPTED}


@dataclass(slots=True)
class Customer:
    tg_id: int
    name: str = ""
    phone: str = ""
    address: str = ""
    username: str | None = None
