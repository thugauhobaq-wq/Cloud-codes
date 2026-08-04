"""Отправка сообщений в Telegram с оглядкой на лимиты."""

from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
)

from .formatter import DIGEST_THRESHOLD, format_digest, format_order
from .models import Order

log = logging.getLogger(__name__)

#: Пауза между сообщениями. У Telegram лимит около 20 сообщений в минуту на
#: канал; три секунды дают запас и не превращают ленту в очередь на полчаса.
MIN_INTERVAL = 3.0

CALLBACK_FAVORITE = "fav"
CALLBACK_STOPWORD = "stop"

_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def order_keyboard(order: Order) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Открыть заказ", url=order.url)],
            [
                InlineKeyboardButton(
                    text="⭐ В избранное", callback_data=f"{CALLBACK_FAVORITE}:{order.uid}"
                ),
                InlineKeyboardButton(
                    text="🔕 Стоп-слово", callback_data=f"{CALLBACK_STOPWORD}:{order.uid}"
                ),
            ],
        ]
    )


class Sender:
    """Очередь отправки: соблюдает паузу и переживает флуд-контроль."""

    def __init__(self, bot: Bot, chat_id: str, *, min_interval: float = MIN_INTERVAL) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._min_interval = min_interval
        self._last_sent_at = 0.0
        self._lock = asyncio.Lock()

    async def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_sent_at
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)
        self._last_sent_at = time.monotonic()

    async def send_text(
        self,
        text: str,
        *,
        chat_id: str | int | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> bool:
        """Отправить готовое сообщение. `False` — если Telegram отказал."""
        async with self._lock:
            await self._throttle()
            target = chat_id if chat_id is not None else self._chat_id

            for attempt in range(2):
                try:
                    await self._bot.send_message(
                        chat_id=target,
                        text=text,
                        parse_mode="HTML",
                        link_preview_options=_NO_PREVIEW,
                        reply_markup=reply_markup,
                    )
                except TelegramRetryAfter as exc:
                    # Флуд-контроль: Telegram сам говорит, сколько ждать.
                    # Спорить бессмысленно, ждём и пробуем ещё раз.
                    if attempt == 0:
                        log.warning("флуд-контроль, жду %s с", exc.retry_after)
                        await asyncio.sleep(exc.retry_after + 1)
                        continue
                    log.error("не отправлено после ожидания флуд-контроля")
                    return False
                except TelegramAPIError as exc:
                    log.error("Telegram отклонил сообщение: %s", exc)
                    return False
                else:
                    self._last_sent_at = time.monotonic()
                    return True

            return False

    async def send_orders(self, orders: list[Order]) -> list[Order]:
        """Разослать заказы и вернуть те, что реально дошли.

        Возвращаем именно список, а не счётчик: вызывающий помечает
        просмотренными только доставленное, поэтому недошедший заказ приедет
        в следующий опрос, а не потеряется навсегда.

        Большие пачки сворачиваются в дайджест: получить двадцать уведомлений
        подряд хуже, чем один список, — и по лимитам, и по нервам.
        """
        if not orders:
            return []

        if len(orders) > DIGEST_THRESHOLD:
            log.info("заказов %d — отправляю дайджестом", len(orders))
            return list(orders) if await self.send_text(format_digest(orders)) else []

        delivered: list[Order] = []
        for order in orders:
            if await self.send_text(format_order(order), reply_markup=order_keyboard(order)):
                delivered.append(order)
        return delivered
