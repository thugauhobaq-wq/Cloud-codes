"""Кнопки под карточкой заказа.

`callback_data` содержит id заказа, а не его содержимое: заказ живёт в базе и
переживает перезапуск, а лимит Telegram — 64 байта.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from botkit.publish import MODE_REPO

from .orders import Order

#: Префикс callback_data: `b:<id>:<действие>`.
PREFIX = "b"

GO = "go"
MODE = "mode"
PRIVACY = "priv"
CANCEL = "no"
RETRY = "again"


def action(build_id: int, name: str) -> str:
    return f"{PREFIX}:{build_id}:{name}"


def order_card(build_id: int, order: Order) -> InlineKeyboardMarkup:
    where = "→ ветка" if order.mode == MODE_REPO else "→ отдельный репозиторий"
    privacy = "→ публичный" if order.private else "→ приватный"

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🚀 Собрать", callback_data=action(build_id, GO)))
    builder.row(
        InlineKeyboardButton(text=where, callback_data=action(build_id, MODE)),
        InlineKeyboardButton(text=privacy, callback_data=action(build_id, PRIVACY)),
    )
    builder.row(InlineKeyboardButton(text="Отмена", callback_data=action(build_id, CANCEL)))
    return builder.as_markup()


def retry(build_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="↻ Повторить", callback_data=action(build_id, RETRY)))
    return builder.as_markup()
