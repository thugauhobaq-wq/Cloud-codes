"""Клавиатуры: список магнитов, проверка подписки, подтверждение рассылки."""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .config import Settings
from .models import Magnet

CB_MAGNET = "mag"
CB_CHECK_SUB = "checksub"
CB_BROADCAST_GO = "bcgo"
CB_BROADCAST_NO = "bcno"
CB_MAGNET_TOGGLE = "magtoggle"
CB_NOOP = "noop"


def magnets_keyboard(magnets: Sequence[Magnet]) -> InlineKeyboardMarkup | None:
    """Кнопки «получить» для готовых магнитов.

    Кодовое слово остаётся основным путём — оно приходит из поста, — но тем,
    кто уже в боте, проще нажать кнопку, чем перепечатывать слово.
    """
    ready = [item for item in magnets if item.ready and item.active]
    if not ready:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=item.title, callback_data=f"{CB_MAGNET}:{item.id}")]
            for item in ready[:10]
        ]
    )


def subscription_keyboard(settings: Settings, magnet: Magnet) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    link = settings.channel_link()
    if link:
        rows.append([InlineKeyboardButton(text="📢 Подписаться", url=link)])
    rows.append(
        [
            InlineKeyboardButton(
                text="✅ Я подписался", callback_data=f"{CB_CHECK_SUB}:{magnet.id}"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def broadcast_confirm_keyboard(count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"📨 Отправить {count}", callback_data=f"{CB_BROADCAST_GO}:"
                ),
                InlineKeyboardButton(text="Отмена", callback_data=f"{CB_BROADCAST_NO}:"),
            ]
        ]
    )


def admin_magnets_keyboard(magnets: Sequence[Magnet]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{'✅' if item.active else '🚫'} {item.code} — {item.title}",
                    callback_data=f"{CB_MAGNET_TOGGLE}:{item.id}",
                )
            ]
            for item in magnets
        ]
    )


def payload(data: str | None) -> str:
    return (data or "").split(":", 1)[1] if ":" in (data or "") else ""
