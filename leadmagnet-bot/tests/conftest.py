from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from leadmagnet.config import Settings
from leadmagnet.storage import Storage


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        bot_token="test",
        bot_username="testbot",
        owner_id=1,
        db_path=str(tmp_path / "test.db"),
        project_name="Тестовая воронка",
        followup_hours=24,
        # В тестах паузы между сообщениями не нужны: проверяем логику, а не темп.
        broadcast_rate=30.0,
    )


@pytest_asyncio.fixture
async def storage(settings: Settings) -> AsyncIterator[Storage]:
    store = Storage(settings.db_path)
    await store.open()
    try:
        yield store
    finally:
        await store.close()


async def add_magnet(storage: Storage, code: str = "чеклист", **kwargs):
    kwargs.setdefault("body", "Вот ваш материал")
    return await storage.add_magnet(code, kwargs.pop("title", "Чек-лист"), **kwargs)
