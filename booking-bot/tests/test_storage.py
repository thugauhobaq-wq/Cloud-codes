from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from booking.models import STATUS_CANCELLED, STATUS_DONE
from booking.storage import SlotTaken, Storage
from conftest import msk


async def make_service(storage: Storage, title: str = "Стрижка", duration: int = 60):
    service_id = await storage.add_service(title, duration, 1500)
    service = await storage.get_service(service_id)
    assert service is not None
    return service


async def test_service_roundtrip(storage: Storage):
    service = await make_service(storage)
    assert service.title == "Стрижка"
    assert service.duration_min == 60
    assert service.active

    await storage.delete_service(service.id)
    assert await storage.list_services() == []
    # Скрытая услуга остаётся в базе: на неё ссылаются прошлые записи.
    assert len(await storage.list_services(only_active=False)) == 1


async def test_create_booking(storage: Storage):
    service = await make_service(storage)
    await storage.upsert_client(1, "Иван", phone="+7 900 000-00-00")

    start = msk(2026, 7, 30, 12)
    booking = await storage.create_booking(client_id=1, service=service, starts_at=start)

    assert booking.starts_at == start
    assert booking.ends_at == start + timedelta(minutes=60)
    assert booking.service_title == "Стрижка"
    assert booking.client_name == "Иван"
    assert booking.is_active


async def test_overlapping_booking_is_rejected(storage: Storage):
    service = await make_service(storage)
    await storage.create_booking(client_id=1, service=service, starts_at=msk(2026, 7, 30, 12))

    with pytest.raises(SlotTaken):
        # Пересечение на полчаса — тот же мастер физически занят.
        await storage.create_booking(
            client_id=2, service=service, starts_at=msk(2026, 7, 30, 12, 30)
        )


async def test_adjacent_booking_is_allowed(storage: Storage):
    service = await make_service(storage)
    await storage.create_booking(client_id=1, service=service, starts_at=msk(2026, 7, 30, 12))
    second = await storage.create_booking(
        client_id=2, service=service, starts_at=msk(2026, 7, 30, 13)
    )
    assert second.is_active


async def test_different_masters_do_not_conflict(storage: Storage):
    service = await make_service(storage)
    anya = await storage.add_master("Аня")
    igor = await storage.add_master("Игорь")

    start = msk(2026, 7, 30, 12)
    await storage.create_booking(client_id=1, service=service, starts_at=start, master_id=anya)
    second = await storage.create_booking(
        client_id=2, service=service, starts_at=start, master_id=igor
    )
    assert second.master_name == "Игорь"


async def test_cancelled_slot_frees_up(storage: Storage):
    service = await make_service(storage)
    start = msk(2026, 7, 30, 12)
    first = await storage.create_booking(client_id=1, service=service, starts_at=start)

    cancelled = await storage.cancel_booking(first.id, by="client")
    assert cancelled is not None
    assert cancelled.status == STATUS_CANCELLED

    # Повторная отмена уже ничего не меняет — кнопку нажали дважды.
    assert await storage.cancel_booking(first.id, by="client") is None

    second = await storage.create_booking(client_id=2, service=service, starts_at=start)
    assert second.is_active


async def test_reschedule_keeps_the_booking_and_clears_the_reminder(storage: Storage):
    service = await make_service(storage)
    booking = await storage.create_booking(
        client_id=1, service=service, starts_at=msk(2026, 7, 30, 12)
    )
    await storage.mark_reminded(booking.id)

    moved = await storage.reschedule_booking(booking.id, msk(2026, 7, 31, 15), service)
    assert moved.id == booking.id
    assert moved.starts_at == msk(2026, 7, 31, 15)
    # Иначе о перенесённой записи никто не напомнит.
    assert moved.reminded_at is None


async def test_reschedule_into_a_taken_slot_fails(storage: Storage):
    service = await make_service(storage)
    first = await storage.create_booking(
        client_id=1, service=service, starts_at=msk(2026, 7, 30, 12)
    )
    await storage.create_booking(client_id=2, service=service, starts_at=msk(2026, 7, 30, 14))

    with pytest.raises(SlotTaken):
        await storage.reschedule_booking(first.id, msk(2026, 7, 30, 14), service)


async def test_reschedule_within_own_time_is_allowed(storage: Storage):
    """Сдвиг на полчаса пересекается сам с собой — и это не должно мешать."""
    service = await make_service(storage)
    booking = await storage.create_booking(
        client_id=1, service=service, starts_at=msk(2026, 7, 30, 12)
    )
    moved = await storage.reschedule_booking(booking.id, msk(2026, 7, 30, 12, 30), service)
    assert moved.starts_at == msk(2026, 7, 30, 12, 30)


async def test_busy_intervals_include_timeoff(storage: Storage):
    service = await make_service(storage)
    await storage.create_booking(client_id=1, service=service, starts_at=msk(2026, 7, 30, 12))
    await storage.add_timeoff(msk(2026, 7, 30, 16), msk(2026, 7, 30, 18), note="учёба")

    busy = await storage.busy_intervals(msk(2026, 7, 30, 0), msk(2026, 7, 31, 0))
    assert len(busy) == 2


async def test_cancelled_booking_is_not_busy(storage: Storage):
    service = await make_service(storage)
    booking = await storage.create_booking(
        client_id=1, service=service, starts_at=msk(2026, 7, 30, 12)
    )
    await storage.cancel_booking(booking.id, by="admin")

    assert await storage.busy_intervals(msk(2026, 7, 30, 0), msk(2026, 7, 31, 0)) == []


async def test_due_reminders_picks_only_the_near_future(storage: Storage):
    service = await make_service(storage)
    now = datetime.now(UTC)
    soon = await storage.create_booking(
        client_id=1, service=service, starts_at=now + timedelta(minutes=45)
    )
    await storage.create_booking(client_id=2, service=service, starts_at=now + timedelta(hours=5))

    due = await storage.due_reminders(now, timedelta(minutes=60))
    assert [item.id for item in due] == [soon.id]

    # Повторно та же запись не приходит — иначе напоминание уйдёт каждую минуту.
    await storage.mark_reminded(soon.id)
    assert await storage.due_reminders(now, timedelta(minutes=60)) == []


async def test_close_past_marks_finished_bookings(storage: Storage):
    service = await make_service(storage)
    now = datetime.now(UTC)
    past = await storage.create_booking(
        client_id=1, service=service, starts_at=now - timedelta(hours=3)
    )
    future = await storage.create_booking(
        client_id=2, service=service, starts_at=now + timedelta(hours=3)
    )

    assert await storage.close_past(now) == 1
    assert (await storage.get_booking(past.id)).status == STATUS_DONE
    assert (await storage.get_booking(future.id)).is_active


async def test_client_phone_is_not_erased_by_a_later_visit(storage: Storage):
    await storage.upsert_client(1, "Иван", phone="+7 900 000-00-00")
    await storage.upsert_client(1, "Иван Петров", phone="")

    client = await storage.get_client(1)
    assert client.name == "Иван Петров"
    assert client.phone == "+7 900 000-00-00"


async def test_set_windows_replaces_instead_of_appending(storage: Storage):
    await storage.set_windows([0, 1], [(600, 1200)])
    await storage.set_windows([0, 1], [(660, 1080)])

    windows = await storage.list_windows()
    assert len(windows) == 2
    assert {item.start_min for item in windows} == {660}


async def test_stats_counts_statuses(storage: Storage):
    service = await make_service(storage)
    now = datetime.now(UTC)
    first = await storage.create_booking(
        client_id=1, service=service, starts_at=now + timedelta(hours=2)
    )
    await storage.create_booking(client_id=2, service=service, starts_at=now + timedelta(hours=4))
    await storage.cancel_booking(first.id, by="client")

    stats = await storage.stats(30)
    assert stats == {"active": 1, "done": 0, "cancelled": 1, "total": 2}
    assert await storage.top_services(30) == [("Стрижка", 1)]
