"""Сборка сообщений для Telegram.

Всё, что пришло с биржи, обязательно экранируется: заголовки заказов регулярно
содержат `<`, `>` и `&`, и без экранирования Bot API отвечает ошибкой разметки —
сообщение просто не доходит.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime

from .models import Order
from .sources import source_titles

DESCRIPTION_LIMIT = 350
#: Порог, после которого пачка заказов уходит одним дайджестом вместо череды
#: отдельных сообщений: два десятка подряд — это уже не уведомление, а спам.
DIGEST_THRESHOLD = 8
DIGEST_MAX_ITEMS = 25

_SOURCE_TITLES = source_titles()


def escape(text: str) -> str:
    return html.escape(text or "", quote=False)


def plural(count: int, one: str, few: str, many: str) -> str:
    """Русская форма множественного числа: 1 отклик, 3 отклика, 7 откликов."""
    if 11 <= count % 100 <= 14:
        return many
    last = count % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def format_amount(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def format_budget(order: Order, currency: str = "₽") -> str:
    low, high = order.budget_min, order.budget_max
    if low is not None and high is not None:
        return f"{format_amount(low)} – {format_amount(high)} {currency}"
    if low is not None:
        return f"от {format_amount(low)} {currency}"
    if high is not None:
        return f"до {format_amount(high)} {currency}"
    return "бюджет не указан"


def humanize_age(moment: datetime | None, *, now: datetime | None = None) -> str | None:
    """«2 мин назад» — возраст заказа важнее точной даты.

    На биржах решает скорость отклика, и понимать, полчаса заказу или трое
    суток, надо мгновенно.
    """
    if moment is None:
        return None

    now = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)

    seconds = (now - moment).total_seconds()
    if seconds < 0:
        return "только что"
    minutes = int(seconds // 60)
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} {plural(hours, 'час', 'часа', 'часов')} назад"
    days = hours // 24
    return f"{days} {plural(days, 'день', 'дня', 'дней')} назад"


def truncate(text: str, limit: int = DESCRIPTION_LIMIT) -> str:
    """Обрезать по границе слова, чтобы не рвать текст посередине."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text

    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,.;:—-") + "…"


def source_title(slug: str) -> str:
    return _SOURCE_TITLES.get(slug, slug)


def format_order(order: Order, *, now: datetime | None = None) -> str:
    """Сообщение об одном заказе."""
    lines = [f"🆕 <b>{escape(order.title)}</b>"]

    head = [f"💰 {format_budget(order)}"]
    if order.category:
        head.append(f"🏷 {escape(order.category)}")
    lines.append(" · ".join(head))

    meta = [f"🌐 {escape(source_title(order.source))}"]
    if order.responses_count is not None:
        count = order.responses_count
        meta.append(f"👥 {count} {plural(count, 'отклик', 'отклика', 'откликов')}")
    age = humanize_age(order.published_at, now=now)
    if age:
        meta.append(f"🕐 {age}")
    lines.append(" · ".join(meta))

    description = truncate(order.description)
    if description:
        lines.append("")
        lines.append(escape(description))

    return "\n".join(lines)


def format_digest(orders: list[Order], *, now: datetime | None = None) -> str:
    """Компактная сводка, когда за один опрос набралось слишком много заказов."""
    total = len(orders)
    header = f"🆕 <b>{total} {plural(total, 'новый заказ', 'новых заказа', 'новых заказов')}</b>"
    lines = [header, ""]

    for index, order in enumerate(orders[:DIGEST_MAX_ITEMS], start=1):
        parts = [format_budget(order), source_title(order.source)]
        age = humanize_age(order.published_at, now=now)
        if age:
            parts.append(age)
        lines.append(
            f"{index}. <a href=\"{escape(order.url)}\">{escape(truncate(order.title, 80))}</a>\n"
            f"    {escape(' · '.join(parts))}"
        )

    hidden = total - DIGEST_MAX_ITEMS
    if hidden > 0:
        lines.append("")
        lines.append(f"…и ещё {hidden} — показаны первые {DIGEST_MAX_ITEMS}.")

    return "\n".join(lines)
