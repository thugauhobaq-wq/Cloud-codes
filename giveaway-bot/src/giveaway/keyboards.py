"""Кнопки и префиксы callback_data."""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .models import Giveaway
from .subscription import channel_link

CB_JOIN = "join"
CB_CHECK = "check"
CB_FINISH = "finish"
CB_CANCEL = "cancel"
CB_PARTICIPANTS = "parts"
CB_REROLL = "reroll"


def join_keyboard(giveaway: Giveaway) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎁 Участвовать", callback_data=f"{CB_JOIN}:{giveaway.id}")]
        ]
    )


def subscribe_keyboard(giveaway: Giveaway, missing: Sequence[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for channel in missing:
        link = channel_link(channel)
        if link:
            rows.append([InlineKeyboardButton(text=f"📢 {channel}", url=link)])
    rows.append(
        [InlineKeyboardButton(text="✅ Я подписался", callback_data=f"{CB_CHECK}:{giveaway.id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_keyboard(giveaway: Giveaway) -> InlineKeyboardMarkup:
    """Кнопки под карточкой у организатора. Набор зависит от состояния."""
    rows: list[list[InlineKeyboardButton]] = []
    if giveaway.is_active:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🏁 Подвести итоги", callback_data=f"{CB_FINISH}:{giveaway.id}"
                ),
                InlineKeyboardButton(
                    text="❌ Отменить", callback_data=f"{CB_CANCEL}:{giveaway.id}"
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="👥 Участники", callback_data=f"{CB_PARTICIPANTS}:{giveaway.id}"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reroll_keyboard(giveaway_id: int, places: Sequence[int]) -> InlineKeyboardMarkup:
    """Замена победителя, который не откликнулся."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🔁 Перевыбрать {place}-е место",
                    callback_data=f"{CB_REROLL}:{giveaway_id}:{place}",
                )
            ]
            for place in places
        ]
    )
