"""Тексты и карточки: витрина, корзина, заказ."""

from __future__ import annotations

from html import escape as _html_escape

from .config import Settings
from .models import (
    DELIVERY_COURIER,
    DELIVERY_TITLES,
    PAY_ONLINE,
    PAY_TITLES,
    STATUS_TITLES,
    Cart,
    Order,
    Product,
)
from .pricing import Totals, fmt_money, totals


def escape(text: str) -> str:
    return _html_escape(text or "", quote=False)


def money(amount: int, settings: Settings) -> str:
    return fmt_money(amount, settings.currency)


def product_card(product: Product, settings: Settings, *, in_cart: int = 0) -> str:
    lines = [f"<b>{escape(product.title)}</b>", money(product.price, settings)]
    if product.description:
        lines.append("")
        lines.append(escape(product.description))
    if settings.track_stock and product.stock is not None:
        lines.append("")
        lines.append(f"Осталось: {product.stock} шт." if product.stock else "Нет в наличии")
    if in_cart:
        lines.append(f"🛒 В корзине: {in_cart} шт.")
    return "\n".join(lines)


def product_line(product: Product, settings: Settings) -> str:
    """Строка списка товаров — коротко, без описания."""
    stock = ""
    if settings.track_stock and product.stock is not None and product.stock <= 3:
        stock = f" · осталось {product.stock}" if product.stock else " · нет в наличии"
    return f"{escape(product.title)} — {money(product.price, settings)}{stock}"


def cart_text(cart: Cart, settings: Settings, *, delivery: str | None = None) -> str:
    if not cart:
        return (
            "🛒 Корзина пуста.\n\nЗагляните в каталог — там всё, что есть в наличии."
        )

    lines = ["🛒 <b>Корзина</b>", ""]
    for line in cart.lines:
        lines.append(
            f"{escape(line.product.title)}\n"
            f"    {line.qty} × {money(line.product.price, settings)} = "
            f"<b>{money(line.total, settings)}</b>"
        )

    lines.append("")
    lines.append(f"Товары: <b>{money(cart.subtotal, settings)}</b>")

    if delivery is not None:
        amounts = totals(cart, settings, delivery)
        lines.append(delivery_line(amounts, settings))
        lines.append(f"Итого: <b>{money(amounts.total, settings)}</b>")
    elif settings.free_delivery_from:
        left = max(0, settings.free_delivery_from - cart.subtotal)
        if left:
            # Показываем, сколько добрать до бесплатной доставки: это самый
            # дешёвый способ поднять средний чек.
            lines.append(f"До бесплатной доставки: {money(left, settings)}")
        else:
            lines.append("Доставка бесплатно 🎉")

    return "\n".join(lines)


def delivery_line(amounts: Totals, settings: Settings) -> str:
    if amounts.free_delivery_applied:
        return "Доставка: <b>бесплатно</b>"
    if not amounts.delivery:
        return "Самовывоз"
    return f"Доставка: <b>{money(amounts.delivery, settings)}</b>"


def order_text(order: Order, settings: Settings, *, for_admin: bool = False) -> str:
    head = f"<b>Заказ №{order.id}</b> · {STATUS_TITLES.get(order.status, order.status)}"
    lines = [head, ""]
    for line in order.lines:
        lines.append(
            f"{escape(line.title)} — {line.qty} × {money(line.price, settings)} = "
            f"<b>{money(line.total, settings)}</b>"
        )

    lines.append("")
    lines.append(f"Товары: {money(order.goods_total, settings)}")
    if order.delivery == DELIVERY_COURIER:
        cost = money(order.delivery_price, settings) if order.delivery_price else "бесплатно"
        lines.append(f"Доставка: {cost}")
    lines.append(f"<b>Итого: {money(order.total, settings)}</b>")

    lines.append("")
    lines.append(f"Способ: {DELIVERY_TITLES.get(order.delivery, order.delivery)}")
    lines.append(f"Оплата: {payment_line(order)}")
    if order.address:
        lines.append(f"📍 {escape(order.address)}")
    if order.comment:
        lines.append(f"💬 {escape(order.comment)}")

    if for_admin:
        lines.append("")
        lines.append(f"👤 {escape(order.name)}")
        lines.append(f"📞 {escape(order.phone)}")
        if order.username:
            lines.append(f"✍️ @{escape(order.username)}")
    return "\n".join(lines)


def payment_line(order: Order) -> str:
    """Как оплачен заказ — первое, что смотрит продавец перед отправкой."""
    method = PAY_TITLES.get(order.payment_method, order.payment_method)
    if order.is_paid:
        return f"✅ оплачено {method}"
    if order.payment_method == PAY_ONLINE:
        return "⏳ ждём оплату картой"
    return f"💵 {method}"


def payment_prompt(order: Order, settings: Settings) -> str:
    provider = ""
    if settings.payment_provider_name:
        provider = f" через {escape(settings.payment_provider_name)}"
    return (
        f"Заказ №{order.id} на <b>{money(order.total, settings)}</b> оформлен.\n\n"
        f"Оплатите картой{provider} — и мы сразу начнём собирать заказ. "
        "Или выберите оплату при получении."
    )


def order_short(order: Order, settings: Settings) -> str:
    when = order.created_at.strftime("%d.%m %H:%M") if order.created_at else ""
    return (
        f"№{order.id} · {when} · {money(order.total, settings)} · "
        f"{STATUS_TITLES.get(order.status, order.status)}"
    )


def welcome(settings: Settings) -> str:
    lines = [f"<b>{escape(settings.shop_name)}</b>"]
    if settings.shop_about:
        lines.append(escape(settings.shop_about))
    lines.append("")
    lines.append("Выберите товар в каталоге, сложите в корзину и оформите заказ.")
    if settings.min_order:
        lines.append(f"Минимальный заказ — {money(settings.min_order, settings)}.")
    if settings.delivery_enabled and settings.free_delivery_from:
        lines.append(
            f"Доставка {money(settings.delivery_price, settings)}, "
            f"от {money(settings.free_delivery_from, settings)} — бесплатно."
        )
    return "\n".join(lines)


def contacts(settings: Settings) -> str:
    lines = [f"<b>{escape(settings.shop_name)}</b>"]
    if settings.contacts:
        lines.append(escape(settings.contacts))
    if settings.pickup_address:
        lines.append(f"📍 Самовывоз: {escape(settings.pickup_address)}")
    if len(lines) == 1:
        lines.append("Контакты пока не заполнены.")
    return "\n".join(lines)
