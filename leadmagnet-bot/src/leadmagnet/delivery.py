"""Выдача магнита и проверка подписки на канал."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from .models import KIND_FILE, KIND_PHOTO, Magnet
from .storage import Storage

log = logging.getLogger(__name__)

#: Статусы участника, при которых считаем человека подписанным.
SUBSCRIBED_STATUSES = {"creator", "administrator", "member", "restricted"}


async def is_subscribed(bot: Bot, channel: str, user_id: int) -> bool:
    """Подписан ли пользователь на канал.

    При ошибке отвечаем «да». Проверка ломается по причинам, в которых
    подписчик не виноват: бота забыли сделать администратором канала, канал
    переименовали, Telegram ответил ошибкой. Закрывать людям доступ к
    обещанному в посте материалу из-за этого — худший из вариантов.
    """
    if not channel:
        return True
    try:
        member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
    except TelegramAPIError as exc:
        log.warning("не смог проверить подписку на %s: %s", channel, exc)
        return True
    return member.status in SUBSCRIBED_STATUSES


class Deliverer:
    """Отправляет материал и записывает факт выдачи."""

    def __init__(self, bot: Bot, storage: Storage) -> None:
        self._bot = bot
        self._storage = storage

    async def send_magnet(self, chat_id: int, magnet: Magnet) -> bool:
        """Отправить содержимое магнита. `False` — отправить не удалось."""
        caption = f"<b>{magnet.title}</b>" if magnet.title else None
        try:
            if magnet.kind == KIND_FILE and magnet.file_id:
                await self._bot.send_document(chat_id, magnet.file_id, caption=caption)
            elif magnet.kind == KIND_PHOTO and magnet.file_id:
                await self._bot.send_photo(chat_id, magnet.file_id, caption=caption)
            else:
                text = magnet.body or magnet.title
                if caption and magnet.body:
                    text = f"{caption}\n\n{magnet.body}"
                await self._bot.send_message(chat_id, text, disable_web_page_preview=False)
        except TelegramAPIError as exc:
            log.warning("не смог выдать магнит %s пользователю %s: %s", magnet.code, chat_id, exc)
            return False
        return True

    async def deliver(self, chat_id: int, magnet: Magnet, source: str = "") -> bool:
        """Выдать магнит и записать выдачу.

        Повторные обращения тоже записываем: человек, вернувшийся за файлом
        через месяц, — живой контакт, и это видно в статистике.
        """
        if not await self.send_magnet(chat_id, magnet):
            return False
        await self._storage.record_delivery(chat_id, magnet.id, source)
        return True
