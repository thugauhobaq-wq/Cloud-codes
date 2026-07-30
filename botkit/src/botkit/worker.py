"""Фоновый цикл: напоминания, догоняющие сообщения, опрос внешних источников.

Очередь работы всегда лежит в базе, а не в таймерах: перезапуск контейнера
убил бы запланированные задачи, а строки в таблице переживают его.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


class PeriodicWorker:
    """Наследник реализует `tick`, каркас берёт на себя цикл и ошибки.

        class Reminders(PeriodicWorker):
            name = "напоминания"

            async def tick(self) -> int:
                ...  # вернуть, сколько сделано
    """

    name = "worker"

    def __init__(self, interval: float, *, name: str | None = None) -> None:
        if interval <= 0:
            raise ValueError("интервал должен быть положительным")
        self.interval = interval
        if name:
            self.name = name
        self.ticks = 0
        self.done_total = 0
        self.last_error: str | None = None

    async def tick(self) -> int:
        """Один проход. Возвращает число выполненных дел — для лога."""
        raise NotImplementedError

    async def run_once(self) -> int:
        done = await self.tick()
        self.ticks += 1
        self.done_total += done
        self.last_error = None
        return done

    async def run_forever(self) -> None:
        log.info("%s: запускаюсь, проверяю раз в %s с", self.name, self.interval)
        while True:
            try:
                done = await self.run_once()
                if done:
                    log.info("%s: сделано %s", self.name, done)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Упавший воркер не должен ронять бота: приём сообщений
                # важнее вовремя разосланных напоминаний.
                self.last_error = str(exc)
                log.exception("%s: сбой в цикле", self.name)
            await asyncio.sleep(self.interval)
