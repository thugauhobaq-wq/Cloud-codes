from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from botkit.testing import FakeBot

from giveaway.config import Settings
from giveaway.notify import Notify
from giveaway.storage import Storage

#: Опорный момент во всех тестах: 1 сентября 2026, 12:00 UTC.
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        bot_token="test",
        owner_id=1,
        bot_username="giveaway_bot",
        db_path=str(tmp_path / "test.db"),
        project_name="Розыгрыши",
        deadline_interval_sec=60,
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
def bot() -> FakeBot:
    """Вместо Telegram — список отправленного."""
    return FakeBot()


@pytest.fixture
def notify(bot: FakeBot, settings: Settings) -> Notify:
    return Notify(bot, settings.admins(), settings)


async def fill(storage: Storage, giveaway_id: int, count: int, *, start: int = 100) -> list[int]:
    """Записать участников с предсказуемыми id."""
    ids = list(range(start, start + count))
    for tg_id in ids:
        await storage.join(giveaway_id, tg_id, name=f"Участник {tg_id}")
    return ids
