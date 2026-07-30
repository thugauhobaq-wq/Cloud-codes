"""Фоновый цикл: напоминания за час до визита и закрытие прошедших записей.

Отдельная задача, а не планировщик на каждую запись: перезапуск контейнера
убил бы все запланированные таймеры, а очередь в базе переживает рестарт.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from .config import Settings
from .keyboards import booking_keyboard
from .notify import Notifier
from .service import BookingService
from .storage import Storage
from .texts import booking_card, escape, human_left

log = logging.getLogger(__name__)


class Reminders:
    def __init__(
        self,
        storage: Storage,
        booking: BookingService,
        notifier: Notifier,
        settings: Settings,
    ) -> None:
        self._storage = storage
        self._booking = booking
        self._notifier = notifier
        self._settings = settings
        self.sent_total = 0
        self.last_error: str | None = None

    async def tick(self, now: datetime | None = None) -> int:
        """Один проход. Возвращает число отправленных напоминаний."""
        now = now or datetime.now(UTC)
        lead = timedelta(minutes=self._settings.reminder_lead_min)

        closed = await self._storage.close_past(now)
        if closed:
            log.debug("закрыл прошедших записей: %s", closed)

        sent = 0
        for item in await self._storage.due_reminders(now, lead):
            text = (
                f"⏰ <b>Напоминание</b>\nЖдём вас через {human_left(item.starts_in(now))}.\n\n"
                + booking_card(item, self._booking.tz)
            )
            if self._settings.business_address:
                text += f"\n📍 {escape(self._settings.business_address)}"

            delivered = await self._notifier.notify_client(
                item.client_id,
                text,
                reply_markup=booking_keyboard(item, can_cancel=self._booking.can_cancel(item, now)),
            )
            # Отметку ставим в любом случае: если клиент заблокировал бота,
            # повторять попытку каждую минуту до самого визита бессмысленно.
            await self._storage.mark_reminded(item.id)
            if delivered:
                sent += 1

        self.sent_total += sent
        return sent

    async def run_forever(self) -> None:
        interval = self._settings.reminder_interval_sec
        log.info(
            "напоминаю за %s мин, проверяю раз в %s с",
            self._settings.reminder_lead_min,
            interval,
        )
        while True:
            try:
                sent = await self.tick()
                if sent:
                    log.info("отправил напоминаний: %s", sent)
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Упавший напоминатель не должен ронять бота: приём записей
                # важнее, чем разосланные вовремя напоминания.
                self.last_error = str(exc)
                log.exception("сбой в цикле напоминаний")
            await asyncio.sleep(interval)
