"""Сценарии записи: связывают хранилище с расчётом слотов.

Хендлеры отсюда получают готовые ответы («вот свободные дни», «запись создана»)
и не занимаются ни арифметикой времени, ни SQL.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from .config import Settings
from .models import Booking, Interval, Service
from .schedule import day_bounds_utc, day_slots, horizon_days
from .storage import SlotTaken, Storage

log = logging.getLogger(__name__)


class BookingService:
    def __init__(self, storage: Storage, settings: Settings) -> None:
        self._storage = storage
        self._settings = settings

    @property
    def tz(self):
        return self._settings.tz

    def today(self, now: datetime | None = None) -> date:
        return (now or datetime.now(UTC)).astimezone(self.tz).date()

    # ── свободное время ───────────────────────────────────────────────────

    async def free_slots(
        self,
        day: date,
        service: Service,
        master_id: int | None = None,
        now: datetime | None = None,
    ) -> list[datetime]:
        now = now or datetime.now(UTC)
        start, end = day_bounds_utc(day, self.tz)
        # Занятость берём с запасом назад: запись, начавшаяся вчера вечером и
        # заходящая за полночь, тоже перекрывает утренние слоты.
        busy = await self._storage.busy_intervals(start - timedelta(hours=12), end, master_id)
        windows = await self._storage.list_windows(master_id)

        return day_slots(
            day=day,
            windows=windows,
            duration_min=service.duration_min,
            busy=busy,
            tz=self.tz,
            now=now,
            step_min=self._settings.slot_step_min,
            min_lead_min=self._settings.min_lead_min,
            master_id=master_id,
        )

    async def calendar(
        self, service: Service, master_id: int | None = None, now: datetime | None = None
    ) -> tuple[list[date], set[date]]:
        """Дни горизонта и подмножество тех, где записаться не на что.

        Один проход по всей занятости вместо запроса на каждый день: при
        горизонте в две недели разница между 1 и 14 обращениями к базе
        заметна на каждом открытии календаря.
        """
        now = now or datetime.now(UTC)
        days = horizon_days(self.today(now), self._settings.horizon_days)
        if not days:
            return [], set()

        span_start, _ = day_bounds_utc(days[0], self.tz)
        _, span_end = day_bounds_utc(days[-1], self.tz)
        busy = await self._storage.busy_intervals(
            span_start - timedelta(hours=12), span_end, master_id
        )
        windows = await self._storage.list_windows(master_id)

        without_slots: set[date] = set()
        for day in days:
            slots = day_slots(
                day=day,
                windows=windows,
                duration_min=service.duration_min,
                busy=self._relevant(busy, day),
                tz=self.tz,
                now=now,
                step_min=self._settings.slot_step_min,
                min_lead_min=self._settings.min_lead_min,
                master_id=master_id,
            )
            if not slots:
                without_slots.add(day)
        return days, without_slots

    def _relevant(self, busy: list[Interval], day: date) -> list[Interval]:
        start, end = day_bounds_utc(day, self.tz)
        return [item for item in busy if item.overlaps(start, end)]

    # ── запись ────────────────────────────────────────────────────────────

    async def book(
        self,
        *,
        client_id: int,
        service: Service,
        starts_at: datetime,
        master_id: int | None = None,
        comment: str = "",
        now: datetime | None = None,
    ) -> Booking:
        """Создать запись, ещё раз проверив, что время не ушло в прошлое.

        Между открытием сетки и нажатием кнопки проходят минуты, и слот к тому
        моменту может перестать быть валидным сразу по двум причинам: его занял
        другой клиент (это ловит хранилище) или он просто наступил.
        """
        now = now or datetime.now(UTC)
        if starts_at < now + timedelta(minutes=self._settings.min_lead_min):
            raise SlotTaken("время уже прошло")

        return await self._storage.create_booking(
            client_id=client_id,
            service=service,
            starts_at=starts_at,
            master_id=master_id,
            comment=comment,
        )

    async def reschedule(
        self, booking: Booking, starts_at: datetime, now: datetime | None = None
    ) -> Booking:
        now = now or datetime.now(UTC)
        if starts_at < now + timedelta(minutes=self._settings.min_lead_min):
            raise SlotTaken("время уже прошло")

        service = await self._storage.get_service(booking.service_id)
        if service is None:
            raise SlotTaken("услуга больше недоступна")
        return await self._storage.reschedule_booking(booking.id, starts_at, service)

    def can_cancel(self, booking: Booking, now: datetime | None = None) -> bool:
        """Можно ли ещё отменить самому.

        Запрет за N минут до начала — не каприз: мастер к этому моменту уже
        отказал другим клиентам на это время.
        """
        now = now or datetime.now(UTC)
        deadline = timedelta(minutes=self._settings.cancel_deadline_min)
        return booking.is_active and booking.starts_at - now > deadline

    # ── дни ───────────────────────────────────────────────────────────────

    async def day_bookings(self, day: date) -> list[Booking]:
        start, end = day_bounds_utc(day, self.tz)
        return await self._storage.bookings_between(start, end)
