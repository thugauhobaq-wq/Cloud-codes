"""Уведомления администраторам и пользователям.

Отдельный слой ради одного правила: недоступный получатель не должен ронять
обработчик. Заказ уже создан, запись уже отменена — падать на отправке письма
об этом поздно и бессмысленно.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, bot: Bot, admins: Iterable[int]) -> None:
        self._bot = bot
        self._admins = sorted(set(admins))

    @property
    def admins(self) -> list[int]:
        return list(self._admins)

    async def notify_admins(self, text: str, *, reply_markup=None) -> int:
        """Разослать админам. Возвращает число доставленных сообщений."""
        delivered = 0
        for admin_id in self._admins:
            if await self.notify(admin_id, text, reply_markup=reply_markup):
                delivered += 1
        return delivered

    async def notify(self, chat_id: int, text: str, *, reply_markup=None, **kwargs) -> bool:
        try:
            await self._bot.send_message(chat_id, text, reply_markup=reply_markup, **kwargs)
            return True
        except TelegramAPIError as exc:
            # Заблокировал бота, удалил чат, неверный id — для отправителя
            # это одинаково: сообщение не дошло, работа продолжается.
            log.info("не доставлено %s: %s", chat_id, exc)
            return False
