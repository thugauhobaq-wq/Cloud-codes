from __future__ import annotations

from datetime import date, timedelta

import pytest

from booking.service import BookingService
from booking.storage import SlotTaken, Storage
from conftest import msk


async def prepare(storage: Storage, *, duration: int = 60):
    service_id = await storage.add_service("Стрижка", duration, 1500)
    await storage.set_windows(range(7), [(10 * 60, 20 * 60)])
    service = await storage.get_service(service_id)
    assert service is not None
    return service


async def test_free_slots_respect_existing_bookings(
    storage: Storage, booking: BookingService
):
    service = await prepare(storage)
    await storage.create_booking(client_id=1, service=service, starts_at=msk(2026, 7, 30, 12))

    slots = await booking.free_slots(date(2026, 7, 30), service, now=msk(2026, 7, 29, 9))
    labels = {item.astimezone(booking.tz).strftime("%H:%M") for item in slots}

    assert "12:00" not in labels
    assert "12:30" not in labels  # заедет на занятый час
    assert "13:00" in labels


async def test_calendar_marks_closed_days(storage: Storage, booking: BookingService):
    service = await prepare(storage)
    # Закрываем послезавтра целиком.
    closed_day = date(2026, 7, 31)
    await storage.add_timeoff(msk(2026, 7, 31, 0), msk(2026, 8, 1, 0), note="выходной")

    days, closed = await booking.calendar(service, now=msk(2026, 7, 29, 9))

    assert days[0] == date(2026, 7, 29)
    assert len(days) == 7
    assert closed_day in closed
    assert date(2026, 7, 30) not in closed


async def test_calendar_closes_today_when_the_day_is_over(
    storage: Storage, booking: BookingService
):
    service = await prepare(storage)
    _days, closed = await booking.calendar(service, now=msk(2026, 7, 29, 21))
    assert date(2026, 7, 29) in closed


async def test_booking_in_the_past_is_refused(storage: Storage, booking: BookingService):
    service = await prepare(storage)
    with pytest.raises(SlotTaken):
        await booking.book(
            client_id=1,
            service=service,
            starts_at=msk(2026, 7, 29, 9),
            now=msk(2026, 7, 29, 12),
        )


async def test_booking_too_close_to_the_start_is_refused(
    storage: Storage, booking: BookingService
):
    """min_lead_min = 60: за полчаса до начала записаться уже нельзя."""
    service = await prepare(storage)
    with pytest.raises(SlotTaken):
        await booking.book(
            client_id=1,
            service=service,
            starts_at=msk(2026, 7, 29, 12, 30),
            now=msk(2026, 7, 29, 12),
        )


async def test_can_cancel_only_before_the_deadline(storage: Storage, booking: BookingService):
    service = await prepare(storage)
    now = msk(2026, 7, 29, 9)
    created = await storage.create_booking(
        client_id=1, service=service, starts_at=now + timedelta(hours=5)
    )
    assert booking.can_cancel(created, now)

    late = await storage.get_booking(created.id)
    # cancel_deadline_min = 120: за час до визита отменять уже поздно.
    assert not booking.can_cancel(late, now + timedelta(hours=4))


async def test_reschedule_through_service_checks_the_lead_time(
    storage: Storage, booking: BookingService
):
    service = await prepare(storage)
    created = await storage.create_booking(
        client_id=1, service=service, starts_at=msk(2026, 7, 30, 12)
    )
    moved = await booking.reschedule(created, msk(2026, 7, 30, 15), now=msk(2026, 7, 29, 9))
    assert moved.starts_at == msk(2026, 7, 30, 15)

    with pytest.raises(SlotTaken):
        await booking.reschedule(moved, msk(2026, 7, 29, 8), now=msk(2026, 7, 29, 9))


async def test_day_bookings_returns_the_local_day(storage: Storage, booking: BookingService):
    service = await prepare(storage)
    await storage.create_booking(client_id=1, service=service, starts_at=msk(2026, 7, 30, 10))
    # Запись в 00:30 по Москве — это ещё 30 июля по UTC, но уже 31-е локально.
    await storage.create_booking(client_id=2, service=service, starts_at=msk(2026, 7, 31, 0, 30))

    assert len(await booking.day_bookings(date(2026, 7, 30))) == 1
    assert len(await booking.day_bookings(date(2026, 7, 31))) == 1
