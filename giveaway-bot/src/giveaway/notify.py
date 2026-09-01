"""Уведомления: победителям, организатору и в канал с итогами."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from botkit import Notifier

from .config import Settings
from .models import Giveaway, Winner
from .texts import announcement, winner_message

log = logging.getLogger(__name__)


class Notify(Notifier):
    """Notifier из botkit плюс объявление итогов."""

    def __init__(self, bot: Bot, admins: Iterable[int], settings: Settings) -> None:
        super().__init__(bot, admins)
        self._bot = bot
        self._settings = settings

    async def announce(self, giveaway: Giveaway, winners: Sequence[Winner]) -> bool:
        """Опубликовать итоги в канал. `False` — публиковать некуда или не вышло.

        Тогда текст уходит организатору: объявление не должно потеряться
        из-за того, что бота не сделали администратором канала.
        """
        text = announcement(giveaway, winners, self._settings)
        chat = self._settings.announce_chat

        if chat:
            try:
                await self._bot.send_message(chat, text, disable_web_page_preview=True)
                return True
            except TelegramAPIError as exc:
                log.warning("не смог опубликовать итоги в %s: %s", chat, exc)
                await self.notify_admins(
                    f"⚠️ Не удалось опубликовать итоги в {chat}: {exc}\n"
                    "Проверьте, что бот — администратор канала. Текст ниже, "
                    "его можно опубликовать вручную."
                )

        await self.notify_admins(text)
        return False

    async def tell_winners(self, giveaway: Giveaway, winners: Sequence[Winner]) -> list[Winner]:
        """Написать победителям. Возвращает тех, до кого не дошло."""
        unreachable: list[Winner] = []
        for winner in winners:
            if not await self.notify(
                winner.tg_id, winner_message(giveaway, winner, self._settings)
            ):
                unreachable.append(winner)
        return unreachable
