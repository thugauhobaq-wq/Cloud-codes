"""Клавиатуры и разбор callback_data.

Telegram ограничивает callback_data 64 байтами, поэтому префиксы короткие, а
время слота кодируется меткой Unix — так дата с часовым поясом укладывается в
десяток символов вместо ISO-строки.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from .models import Booking, Master, Service
from .texts import fmt_day_label, fmt_price, human_duration

# ── префиксы callback_data ────────────────────────────────────────────────
CB_SERVICE = "svc"
CB_MASTER = "mst"
CB_DAY = "day"
CB_SLOT = "slot"
CB_CONFIRM = "ok"
CB_CANCEL = "cancel"
CB_CANCEL_YES = "cyes"
CB_MOVE = "move"
CB_BACK = "back"
CB_NOOP = "noop"
CB_ADMIN_CANCEL = "acancel"

BTN_BOOK = "📅 Записаться"
BTN_MY = "🗂 Мои записи"
BTN_SERVICES = "💇 Услуги и цены"
BTN_CONTACTS = "📍 Контакты"

#: Кнопок со слотами в ряду. Три по «10:00» — предел, при котором подпись
#: не обрезается на узких экранах.
SLOTS_PER_ROW = 3
DAYS_PER_ROW = 3


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_BOOK), KeyboardButton(text=BTN_MY)],
            [KeyboardButton(text=BTN_SERVICES), KeyboardButton(text=BTN_CONTACTS)],
        ],
        resize_keyboard=True,
    )


def request_phone() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📞 Отправить телефон", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def services_keyboard(services: Sequence[Service]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{item.title} · {human_duration(item.duration_min)}"
                + (f" · {fmt_price(item.price)}" if item.price else ""),
                callback_data=f"{CB_SERVICE}:{item.id}",
            )
        ]
        for item in services
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def masters_keyboard(masters: Sequence[Master]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=item.name, callback_data=f"{CB_MASTER}:{item.id}")]
        for item in masters
    ]
    # «Не важно» — не вежливость, а способ показать объединённую сетку слотов:
    # у клиента, которому всё равно, выбор шире.
    rows.append([InlineKeyboardButton(text="Не важно", callback_data=f"{CB_MASTER}:0")])
    rows.append([InlineKeyboardButton(text="⬅️ К услугам", callback_data=f"{CB_BACK}:service")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def days_keyboard(
    days: Sequence[date], busy_days: set[date], today: date, *, with_back: bool
) -> InlineKeyboardMarkup:
    """Календарь на горизонт записи. Дни без слотов остаются видимыми, но не нажимаются."""
    buttons: list[InlineKeyboardButton] = []
    for day in days:
        free = day not in busy_days
        buttons.append(
            InlineKeyboardButton(
                text=fmt_day_label(day, today) if free else f"✖️ {fmt_day_label(day, today)}",
                callback_data=f"{CB_DAY}:{day.isoformat()}" if free else f"{CB_NOOP}:",
            )
        )

    rows = [buttons[i : i + DAYS_PER_ROW] for i in range(0, len(buttons), DAYS_PER_ROW)]
    if with_back:
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"{CB_BACK}:master")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def slots_keyboard(slots: Sequence[datetime], tz: ZoneInfo) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=slot.astimezone(tz).strftime("%H:%M"),
            callback_data=f"{CB_SLOT}:{int(slot.timestamp())}",
        )
        for slot in slots
    ]
    rows = [buttons[i : i + SLOTS_PER_ROW] for i in range(0, len(buttons), SLOTS_PER_ROW)]
    rows.append([InlineKeyboardButton(text="⬅️ К дате", callback_data=f"{CB_BACK}:day")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Записаться", callback_data=f"{CB_CONFIRM}:")],
            [InlineKeyboardButton(text="⬅️ Другое время", callback_data=f"{CB_BACK}:day")],
        ]
    )


def booking_keyboard(booking: Booking, *, can_cancel: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="🔄 Перенести", callback_data=f"{CB_MOVE}:{booking.id}")]]
    if can_cancel:
        rows.append(
            [InlineKeyboardButton(text="❌ Отменить", callback_data=f"{CB_CANCEL}:{booking.id}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_confirm_keyboard(booking_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, отменить", callback_data=f"{CB_CANCEL_YES}:{booking_id}"
                ),
                InlineKeyboardButton(text="Нет", callback_data=f"{CB_NOOP}:"),
            ]
        ]
    )


def admin_booking_keyboard(booking_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отменить запись", callback_data=f"{CB_ADMIN_CANCEL}:{booking_id}"
                )
            ]
        ]
    )


def payload(data: str | None) -> str:
    """Часть callback_data после префикса."""
    return (data or "").split(":", 1)[1] if ":" in (data or "") else ""


def slot_from_payload(raw: str) -> datetime | None:
    if not raw.lstrip("-").isdigit():
        return None
    return datetime.fromtimestamp(int(raw), UTC)
