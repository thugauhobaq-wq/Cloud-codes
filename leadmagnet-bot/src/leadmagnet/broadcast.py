"""Рассылка по базе подписчиков.

Отправка вынесена в подставляемую функцию: движок с его темпом, подсчётом и
обработкой блокировок так проверяется тестами без Telegram.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError as _TelegramAPIError,
)
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from .config import Settings
from .models import Broadcast, Lead
from .storage import Storage

log = logging.getLogger(__name__)

SENT = "sent"
BLOCKED = "blocked"
FAILED = "failed"

#: Куда слать: (chat_id, текст) → один из статусов выше.
Sender = Callable[[int, str], Awaitable[str]]


@dataclass(slots=True)
class Progress:
    total: int
    sent: int
    failed: int
    blocked: int


def telegram_sender(bot: Bot) -> Sender:
    """Отправка через Telegram с разбором типичных отказов."""

    async def send(chat_id: int, text: str) -> str:
        try:
            await bot.send_message(chat_id, text, disable_web_page_preview=True)
            return SENT
        except TelegramForbiddenError:
            # Пользователь заблокировал бота — единственный отказ, из-за
            # которого стоит помечать контакт и больше ему не писать.
            return BLOCKED
        except TelegramRetryAfter as exc:
            # Telegram сам говорит, сколько подождать; после паузы пробуем ещё раз.
            log.info("притормаживаю на %s с по требованию Telegram", exc.retry_after)
            await asyncio.sleep(exc.retry_after)
            try:
                await bot.send_message(chat_id, text, disable_web_page_preview=True)
                return SENT
            except _TelegramAPIError:
                return FAILED
        except _TelegramAPIError as exc:
            log.debug("не доставлено %s: %s", chat_id, exc)
            return FAILED

    return send


class Broadcaster:
    def __init__(self, storage: Storage, settings: Settings, sender: Sender) -> None:
        self._storage = storage
        self._settings = settings
        self._send = sender

    async def audience(self, magnet_code: str = "") -> list[Lead]:
        return await self._storage.list_leads(magnet_code=magnet_code)

    async def run(
        self,
        text: str,
        *,
        magnet_code: str = "",
        on_progress: Callable[[Progress], Awaitable[None]] | None = None,
        progress_every: int = 50,
    ) -> Broadcast:
        """Разослать текст. Возвращает запись рассылки с итогами."""
        leads = await self.audience(magnet_code)
        record = await self._storage.create_broadcast(text, magnet_code, len(leads))

        # Пауза между сообщениями держит темп ниже лимита Telegram: превысишь —
        # и рассылку срежут на середине, а половина базы останется без письма.
        delay = 1 / self._settings.broadcast_rate if self._settings.broadcast_rate else 0
        progress = Progress(total=len(leads), sent=0, failed=0, blocked=0)

        for index, lead in enumerate(leads, start=1):
            result = await self._send(lead.tg_id, text)
            if result == SENT:
                progress.sent += 1
            elif result == BLOCKED:
                progress.blocked += 1
                await self._storage.mark_blocked(lead.tg_id)
            else:
                progress.failed += 1

            if on_progress and index % progress_every == 0:
                await on_progress(progress)
            if delay and index < len(leads):
                await asyncio.sleep(delay)

        await self._storage.finish_broadcast(
            record.id, progress.sent, progress.failed + progress.blocked
        )
        if on_progress:
            await on_progress(progress)

        log.info(
            "рассылка #%s: отправлено %s, заблокировали %s, ошибок %s",
            record.id,
            progress.sent,
            progress.blocked,
            progress.failed,
        )
        result_record = await self._storage.get_broadcast(record.id)
        return result_record or record
