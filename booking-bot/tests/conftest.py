from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from booking.config import Settings
from booking.models import WorkWindow
from booking.service import BookingService
from booking.storage import Storage

MSK = ZoneInfo("Europe/Moscow")

#: Опорный момент во всех тестах: среда, 29 июля 2026, 09:00 по Москве.
NOW = datetime(2026, 7, 29, 6, 0, tzinfo=UTC)


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def tz() -> ZoneInfo:
    return MSK


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        bot_token="test",
        owner_id=1,
        timezone="Europe/Moscow",
        db_path=str(tmp_path / "test.db"),
        horizon_days=7,
        slot_step_min=30,
        min_lead_min=60,
        cancel_deadline_min=120,
        reminder_lead_min=60,
    )


@pytest_asyncio.fixture
async def storage(settings: Settings) -> AsyncIterator[Storage]:
    store = Storage(settings.db_path)
    await store.open()
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
def booking(storage: Storage, settings: Settings) -> BookingService:
    return BookingService(storage, settings)


def workday(start_hour: int = 10, end_hour: int = 20, weekdays=range(7)) -> list[WorkWindow]:
    return [
        WorkWindow(weekday=day, start_min=start_hour * 60, end_min=end_hour * 60)
        for day in weekdays
    ]


def msk(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Момент по московскому времени, приведённый к UTC."""
    return datetime(year, month, day, hour, minute, tzinfo=MSK).astimezone(UTC)
