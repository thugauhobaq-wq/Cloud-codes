"""Клавиатуры витрины, корзины и оформления заказа."""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from .config import Settings
from .models import DELIVERY_COURIER, DELIVERY_PICKUP, Cart, Category, Product
from .texts import money

# ── префиксы callback_data ────────────────────────────────────────────────
CB_CATEGORY = "cat"
CB_PAGE = "page"
CB_PRODUCT = "prod"
CB_ADD = "add"
CB_INC = "inc"
CB_DEC = "dec"
CB_CART_INC = "cinc"
CB_CART_DEC = "cdec"
CB_DROP = "drop"
CB_CLEAR = "clear"
CB_CART = "cart"
CB_CHECKOUT = "checkout"
CB_DELIVERY = "dlv"
CB_CONFIRM = "confirm"
CB_CATALOG = "catalog"
CB_ORDER_STATUS = "ostatus"
CB_NOOP = "noop"

BTN_CATALOG = "🛍 Каталог"
BTN_CART = "🛒 Корзина"
BTN_ORDERS = "📦 Мои заказы"
BTN_CONTACTS = "📞 Контакты"


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_CATALOG), KeyboardButton(text=BTN_CART)],
            [KeyboardButton(text=BTN_ORDERS), KeyboardButton(text=BTN_CONTACTS)],
        ],
        resize_keyboard=True,
    )


def request_phone() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📞 Отправить телефон", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def categories_keyboard(categories: Sequence[Category], cart: Cart) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=item.title, callback_data=f"{CB_CATEGORY}:{item.id}")]
        for item in categories
    ]
    if cart:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🛒 Корзина · {cart.count} шт.", callback_data=f"{CB_CART}:"
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def products_keyboard(
    products: Sequence[Product],
    settings: Settings,
    *,
    category_id: int | None,
    page: int,
    pages: int,
    cart: Cart,
    with_back: bool = True,
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=_product_button(item, settings, cart),
                callback_data=f"{CB_PRODUCT}:{item.id}",
            )
        ]
        for item in products
    ]

    if pages > 1:
        # Стрелки листания живут в одном ряду со счётчиком страниц: без него
        # непонятно, докуда листать.
        nav = [
            InlineKeyboardButton(
                text="◀️" if page > 0 else " ",
                callback_data=f"{CB_PAGE}:{category_id or 0}:{page - 1}"
                if page > 0
                else f"{CB_NOOP}:",
            ),
            InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data=f"{CB_NOOP}:"),
            InlineKeyboardButton(
                text="▶️" if page + 1 < pages else " ",
                callback_data=f"{CB_PAGE}:{category_id or 0}:{page + 1}"
                if page + 1 < pages
                else f"{CB_NOOP}:",
            ),
        ]
        rows.append(nav)

    tail: list[InlineKeyboardButton] = []
    if with_back:
        tail.append(InlineKeyboardButton(text="⬅️ Категории", callback_data=f"{CB_CATALOG}:"))
    if cart:
        tail.append(
            InlineKeyboardButton(text=f"🛒 {cart.count} шт.", callback_data=f"{CB_CART}:")
        )
    if tail:
        rows.append(tail)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _product_button(product: Product, settings: Settings, cart: Cart) -> str:
    line = cart.line(product.id)
    mark = f" · 🛒{line.qty}" if line else ""
    return f"{product.title} — {money(product.price, settings)}{mark}"


def product_keyboard(
    product: Product, cart: Cart, *, category_id: int | None, page: int
) -> InlineKeyboardMarkup:
    line = cart.line(product.id)
    rows: list[list[InlineKeyboardButton]] = []

    if line:
        rows.append(
            [
                InlineKeyboardButton(text="➖", callback_data=f"{CB_DEC}:{product.id}"),
                InlineKeyboardButton(text=f"{line.qty} шт.", callback_data=f"{CB_NOOP}:"),
                InlineKeyboardButton(text="➕", callback_data=f"{CB_INC}:{product.id}"),
            ]
        )
    else:
        rows.append(
            [InlineKeyboardButton(text="🛒 В корзину", callback_data=f"{CB_ADD}:{product.id}")]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ К списку",
                callback_data=f"{CB_PAGE}:{category_id or 0}:{page}",
            ),
            InlineKeyboardButton(text="🛒 Корзина", callback_data=f"{CB_CART}:"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cart_keyboard(cart: Cart) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for line in cart.lines:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{line.product.title[:24]} · {line.qty} шт.",
                    callback_data=f"{CB_PRODUCT}:{line.product.id}",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(text="➖", callback_data=f"{CB_CART_DEC}:{line.product.id}"),
                InlineKeyboardButton(text="➕", callback_data=f"{CB_CART_INC}:{line.product.id}"),
                InlineKeyboardButton(text="🗑", callback_data=f"{CB_DROP}:{line.product.id}"),
            ]
        )

    if cart:
        rows.append(
            [InlineKeyboardButton(text="✅ Оформить заказ", callback_data=f"{CB_CHECKOUT}:")]
        )
        rows.append(
            [
                InlineKeyboardButton(text="🛍 Каталог", callback_data=f"{CB_CATALOG}:"),
                InlineKeyboardButton(text="Очистить", callback_data=f"{CB_CLEAR}:"),
            ]
        )
    else:
        rows.append([InlineKeyboardButton(text="🛍 В каталог", callback_data=f"{CB_CATALOG}:")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def delivery_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if settings.pickup_address:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🏬 Самовывоз", callback_data=f"{CB_DELIVERY}:{DELIVERY_PICKUP}"
                )
            ]
        )
    if settings.delivery_enabled:
        title = "🚚 Доставка"
        if settings.delivery_price:
            title += f" · {money(settings.delivery_price, settings)}"
        rows.append(
            [InlineKeyboardButton(text=title, callback_data=f"{CB_DELIVERY}:{DELIVERY_COURIER}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить заказ", callback_data=f"{CB_CONFIRM}:")],
            [InlineKeyboardButton(text="⬅️ В корзину", callback_data=f"{CB_CART}:")],
        ]
    )


def skip_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Пропустить")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def order_admin_keyboard(order_id: int, status: str) -> InlineKeyboardMarkup:
    """Кнопки под заказом у владельца. Набор зависит от текущего статуса."""
    rows: list[list[InlineKeyboardButton]] = []
    if status == "new":
        rows.append(
            [
                InlineKeyboardButton(
                    text="✅ Принять", callback_data=f"{CB_ORDER_STATUS}:{order_id}:accepted"
                ),
                InlineKeyboardButton(
                    text="❌ Отменить", callback_data=f"{CB_ORDER_STATUS}:{order_id}:cancelled"
                ),
            ]
        )
    elif status == "accepted":
        rows.append(
            [
                InlineKeyboardButton(
                    text="📦 Выполнен", callback_data=f"{CB_ORDER_STATUS}:{order_id}:done"
                ),
                InlineKeyboardButton(
                    text="❌ Отменить", callback_data=f"{CB_ORDER_STATUS}:{order_id}:cancelled"
                ),
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payload(data: str | None) -> str:
    return (data or "").split(":", 1)[1] if ":" in (data or "") else ""


def page_count(total: int, page_size: int) -> int:
    if total <= 0:
        return 0
    return (total + page_size - 1) // page_size
