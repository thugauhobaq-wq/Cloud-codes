from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from booking.config import Settings
from booking.reminders import Reminders
from booking.service import BookingService
from booking.storage import Storage


@dataclass
class FakeNotifier:
    """Вместо Telegram — список отправленного; `delivered` имитирует блокировку бота."""

    delivered: bool = True
    sent: list[tuple[int, str]] = field(default_factory=list)

    async def notify_client(self, chat_id: int, text: str, *, reply_markup=None) -> bool:
        self.sent.append((chat_id, text))
        return self.delivered


async def prepare(storage: Storage, minutes_ahead: int, now: datetime):
    service_id = await storage.add_service("Стрижка", 60, 1500)
    service = await storage.get_service(service_id)
    await storage.upsert_client(42, "Иван", phone="+7 900 000-00-00")
    return await storage.create_booking(
        client_id=42, service=service, starts_at=now + timedelta(minutes=minutes_ahead)
    )


async def test_reminder_goes_out_within_the_lead_window(
    storage: Storage, booking: BookingService, settings: Settings
):
    now = datetime.now(UTC)
    await prepare(storage, 45, now)
    notifier = FakeNotifier()

    sent = await Reminders(storage, booking, notifier, settings).tick(now)

    assert sent == 1
    chat_id, text = notifier.sent[0]
    assert chat_id == 42
    assert "Стрижка" in text
    assert "45 мин" in text


async def test_reminder_is_not_repeated(
    storage: Storage, booking: BookingService, settings: Settings
):
    now = datetime.now(UTC)
    await prepare(storage, 45, now)
    notifier = FakeNotifier()
    reminders = Reminders(storage, booking, notifier, settings)

    await reminders.tick(now)
    assert await reminders.tick(now) == 0


async def test_distant_booking_waits_its_turn(
    storage: Storage, booking: BookingService, settings: Settings
):
    now = datetime.now(UTC)
    await prepare(storage, 5 * 60, now)
    notifier = FakeNotifier()

    assert await Reminders(storage, booking, notifier, settings).tick(now) == 0
    assert notifier.sent == []


async def test_blocked_client_is_marked_anyway(
    storage: Storage, booking: BookingService, settings: Settings
):
    """Клиент заблокировал бота — попытку повторять до самого визита незачем."""
    now = datetime.now(UTC)
    created = await prepare(storage, 45, now)
    notifier = FakeNotifier(delivered=False)
    reminders = Reminders(storage, booking, notifier, settings)

    assert await reminders.tick(now) == 0
    assert (await storage.get_booking(created.id)).reminded_at is not None
    assert await reminders.tick(now) == 0


async def test_tick_closes_finished_bookings(
    storage: Storage, booking: BookingService, settings: Settings
):
    now = datetime.now(UTC)
    created = await prepare(storage, -180, now)

    await Reminders(storage, booking, FakeNotifier(), settings).tick(now)

    assert (await storage.get_booking(created.id)).status == "done"


async def test_cancelled_booking_gets_no_reminder(
    storage: Storage, booking: BookingService, settings: Settings
):
    now = datetime.now(UTC)
    created = await prepare(storage, 45, now)
    await storage.cancel_booking(created.id, by="client")
    notifier = FakeNotifier()

    assert await Reminders(storage, booking, notifier, settings).tick(now) == 0
