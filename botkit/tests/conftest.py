from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from botkit import BaseStorage, BotSettings


class DemoStorage(BaseStorage):
    """Минимальное хранилище для проверки каркаса."""

    schema = """
    CREATE TABLE IF NOT EXISTS notes (
        id    INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        done  INTEGER NOT NULL DEFAULT 0
    );
    """


@pytest.fixture
def settings(tmp_path: Path) -> BotSettings:
    return BotSettings(bot_token="test", owner_id=1, db_path=str(tmp_path / "test.db"))


@pytest_asyncio.fixture
async def storage(settings: BotSettings) -> AsyncIterator[DemoStorage]:
    store = DemoStorage(settings.db_path)
    await store.open()
    try:
        yield store
    finally:
        await store.close()
