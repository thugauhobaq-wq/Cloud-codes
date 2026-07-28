"""Догоняющее сообщение через N часов после выдачи.

Второе касание — половина смысла воронки: человек скачал чек-лист и забыл про
него, а через сутки получает напоминание с предложением. Очередь лежит в базе,
поэтому перезапуск контейнера ничего не теряет.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from .broadcast import BLOCKED, SENT, Sender
from .config import Settings
from .storage import Storage

log = logging.getLogger(__name__)


class FollowUps:
    def __init__(self, storage: Storage, settings: Settings, sender: Sender) -> None:
        self._storage = storage
        self._settings = settings
        self._send = sender
        self.sent_total = 0
        self.last_error: str | None = None

    async def tick(self, now: datetime | None = None) -> int:
        """Один проход. Возвращает число отправленных сообщений."""
        if not self._settings.followup_enabled:
            return 0

        now = now or datetime.now(UTC)
        delay = timedelta(hours=self._settings.followup_hours)
        sent = 0

        for delivery in await self._storage.due_followups(now, delay):
            magnet = await self._storage.get_magnet(delivery.magnet_id)
            if magnet is None or not magnet.followup:
                await self._storage.mark_followup_sent(delivery.id)
                continue

            result = await self._send(delivery.lead_id, magnet.followup)
            # Отмечаем в любом случае: если человек заблокировал бота, повторять
            # попытку каждые пять минут бессмысленно.
            await self._storage.mark_followup_sent(delivery.id)
            if result == BLOCKED:
                await self._storage.mark_blocked(delivery.lead_id)
            elif result == SENT:
                sent += 1

        self.sent_total += sent
        return sent

    async def run_forever(self) -> None:
        interval = self._settings.followup_interval_sec
        if not self._settings.followup_enabled:
            log.info("догоняющие сообщения выключены")
            return

        log.info(
            "догоняю через %s ч, проверяю раз в %s с", self._settings.followup_hours, interval
        )
        while True:
            try:
                sent = await self.tick()
                if sent:
                    log.info("отправил догоняющих: %s", sent)
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Упавший догон не должен ронять бота: выдача магнитов важнее.
                self.last_error = str(exc)
                log.exception("сбой в цикле догоняющих сообщений")
            await asyncio.sleep(interval)
