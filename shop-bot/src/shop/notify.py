"""Уведомления владельцу о заказах и покупателю — о смене статуса."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from .config import Settings
from .keyboards import order_admin_keyboard
from .models import STATUS_TITLES, Order
from .texts import money, order_text

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, bot: Bot, admins: Iterable[int], settings: Settings) -> None:
        self._bot = bot
        self._admins = sorted(set(admins))
        self._settings = settings

    async def send_admins(self, text: str, *, reply_markup=None) -> None:
        for admin_id in self._admins:
            try:
                await self._bot.send_message(admin_id, text, reply_markup=reply_markup)
            except TelegramAPIError as exc:
                # Один недоступный админ не должен мешать остальным узнать о заказе.
                log.warning("не смог уведомить %s: %s", admin_id, exc)

    async def new_order(self, order: Order) -> None:
        await self.send_admins(
            "🔔 <b>Новый заказ</b>\n\n" + order_text(order, self._settings, for_admin=True),
            reply_markup=order_admin_keyboard(order.id, order.status),
        )

    async def low_stock(self, titles: list[str]) -> None:
        if not titles:
            return
        await self.send_admins(
            "⚠️ <b>Заканчивается</b>\n" + "\n".join(f"• {title}" for title in titles)
        )

    async def notify_customer(self, chat_id: int, text: str) -> bool:
        try:
            await self._bot.send_message(chat_id, text)
            return True
        except TelegramAPIError as exc:
            log.info("покупатель %s недоступен: %s", chat_id, exc)
            return False

    async def order_status(self, order: Order) -> bool:
        """Сообщить покупателю о новом статусе заказа."""
        title = STATUS_TITLES.get(order.status, order.status)
        text = f"Заказ №{order.id}: <b>{title}</b>\nСумма: {money(order.total, self._settings)}"
        if order.status == "accepted":
            text += "\n\nПродавец принял заказ и скоро свяжется с вами."
        elif order.status == "cancelled":
            text += "\n\nЕсли это ошибка, напишите нам."
        return await self.notify_customer(order.customer_id, text)
