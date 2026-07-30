"""Рассылка по базе с темпом ниже лимита Telegram.

Отправка вынесена в подставляемую функцию: движок с его темпом, подсчётами и
обработкой блокировок проверяется тестами без сети.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter

log = logging.getLogger(__name__)

SENT = "sent"
BLOCKED = "blocked"
FAILED = "failed"

#: Куда слать: (chat_id, текст) → один из статусов выше.
Sender = Callable[[int, str], Awaitable[str]]

#: Telegram гасит рассылку примерно на 30 сообщениях в секунду. 20 — запас,
#: при котором бота не ограничивают даже на пиках.
DEFAULT_RATE = 20.0


@dataclass(slots=True)
class Progress:
    total: int
    sent: int = 0
    blocked: int = 0
    failed: int = 0

    @property
    def processed(self) -> int:
        return self.sent + self.blocked + self.failed


def telegram_sender(bot: Bot, **kwargs) -> Sender:
    """Отправка через Telegram с разбором типичных отказов."""

    async def send(chat_id: int, text: str) -> str:
        try:
            await bot.send_message(chat_id, text, **kwargs)
            return SENT
        except TelegramForbiddenError:
            # Пользователь заблокировал бота — единственный отказ, из-за
            # которого стоит пометить контакт и больше ему не писать.
            return BLOCKED
        except TelegramRetryAfter as exc:
            # Telegram сам говорит, сколько подождать; после паузы пробуем ещё раз.
            log.info("притормаживаю на %s с по требованию Telegram", exc.retry_after)
            await asyncio.sleep(exc.retry_after)
            try:
                await bot.send_message(chat_id, text, **kwargs)
                return SENT
            except TelegramAPIError:
                return FAILED
        except TelegramAPIError as exc:
            log.debug("не доставлено %s: %s", chat_id, exc)
            return FAILED

    return send


class Broadcaster:
    def __init__(
        self,
        sender: Sender,
        *,
        rate: float = DEFAULT_RATE,
        on_blocked: Callable[[int], Awaitable[None]] | None = None,
    ) -> None:
        self._send = sender
        self._rate = rate
        self._on_blocked = on_blocked

    async def run(
        self,
        recipients: Sequence[int],
        text: str,
        *,
        on_progress: Callable[[Progress], Awaitable[None]] | None = None,
        progress_every: int = 50,
    ) -> Progress:
        """Разослать текст. Возвращает итоги — их обычно показывают владельцу."""
        progress = Progress(total=len(recipients))
        # Пауза между сообщениями держит темп ниже лимита: превысишь — и
        # рассылку срежут на середине, а половина базы останется без письма.
        delay = 1 / self._rate if self._rate else 0

        for index, chat_id in enumerate(recipients, start=1):
            result = await self._send(chat_id, text)
            if result == SENT:
                progress.sent += 1
            elif result == BLOCKED:
                progress.blocked += 1
                if self._on_blocked is not None:
                    await self._on_blocked(chat_id)
            else:
                progress.failed += 1

            if on_progress and index % progress_every == 0:
                await on_progress(progress)
            if delay and index < len(recipients):
                await asyncio.sleep(delay)

        if on_progress:
            await on_progress(progress)

        log.info(
            "рассылка: отправлено %s из %s, заблокировали %s, ошибок %s",
            progress.sent,
            progress.total,
            progress.blocked,
            progress.failed,
        )
        return progress
