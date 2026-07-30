"""Уведомления администраторам о новых записях, отменах и переносах."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from .keyboards import admin_booking_keyboard
from .models import Booking
from .texts import booking_card

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, bot: Bot, admins: Iterable[int], tz: ZoneInfo) -> None:
        self._bot = bot
        self._admins = sorted(set(admins))
        self._tz = tz

    async def send(self, text: str, *, reply_markup=None) -> None:
        for admin_id in self._admins:
            try:
                await self._bot.send_message(admin_id, text, reply_markup=reply_markup)
            except TelegramAPIError as exc:
                # Один админ, заблокировавший бота, не должен мешать остальным
                # получить уведомление о записи.
                log.warning("не смог уведомить %s: %s", admin_id, exc)

    async def new_booking(self, booking: Booking) -> None:
        await self.send(
            "🆕 <b>Новая запись</b>\n\n" + booking_card(booking, self._tz, with_client=True),
            reply_markup=admin_booking_keyboard(booking.id),
        )

    async def cancelled(self, booking: Booking, *, by_client: bool) -> None:
        who = "клиент" if by_client else "администратор"
        await self.send(
            f"❌ <b>Запись отменена</b> ({who})\n\n"
            + booking_card(booking, self._tz, with_client=True)
        )

    async def moved(self, booking: Booking, previous: str) -> None:
        await self.send(
            f"🔄 <b>Перенос</b>\nБыло: {previous}\n\n"
            + booking_card(booking, self._tz, with_client=True)
        )

    async def notify_client(self, chat_id: int, text: str, *, reply_markup=None) -> bool:
        try:
            await self._bot.send_message(chat_id, text, reply_markup=reply_markup)
            return True
        except TelegramAPIError as exc:
            log.info("клиент %s недоступен: %s", chat_id, exc)
            return False
