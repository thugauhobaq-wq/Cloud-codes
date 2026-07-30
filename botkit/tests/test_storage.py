from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from botkit import BotSettings, from_iso, to_iso
from botkit.storage import rulower
from conftest import DemoStorage


async def test_schema_of_the_subclass_is_applied(storage: DemoStorage):
    note_id = await storage.execute("INSERT INTO notes (title) VALUES (?)", ("Первая",))
    assert note_id == 1

    row = await storage.fetch_one("SELECT * FROM notes WHERE id = ?", (note_id,))
    assert row["title"] == "Первая"


async def test_database_file_is_created_with_missing_parents(tmp_path: Path):
    path = tmp_path / "deep" / "nested" / "bot.db"
    async with DemoStorage(str(path)):
        pass
    assert path.exists()


async def test_using_storage_before_open_says_what_is_wrong():
    with pytest.raises(RuntimeError, match="open"):
        _ = DemoStorage("/tmp/never.db").connection


async def test_execute_changes_tells_whether_anything_happened(storage: DemoStorage):
    note_id = await storage.execute("INSERT INTO notes (title) VALUES (?)", ("Дело",))

    assert await storage.execute_changes("UPDATE notes SET done = 1 WHERE id = ?", (note_id,)) == 1
    # Строки уже нет — значит, кнопку нажали второй раз.
    assert await storage.execute_changes("UPDATE notes SET done = 1 WHERE id = 999") == 0


async def test_executemany_ignores_an_empty_batch(storage: DemoStorage):
    await storage.executemany("INSERT INTO notes (title) VALUES (?)", [])
    assert await storage.fetch_value("SELECT COUNT(*) FROM notes") == 0


async def test_fetch_value_returns_the_default(storage: DemoStorage):
    assert await storage.fetch_value("SELECT title FROM notes WHERE id = 1", (), "нет") == "нет"


async def test_transaction_commits(storage: DemoStorage):
    async with storage.transaction() as conn:
        await conn.execute("INSERT INTO notes (title) VALUES (?)", ("В транзакции",))

    assert await storage.fetch_value("SELECT COUNT(*) FROM notes") == 1


async def test_transaction_rolls_back_on_error(storage: DemoStorage):
    """Проверка и запись одним куском: если проверка не прошла, не остаётся ничего."""

    class SlotTaken(RuntimeError):
        pass

    with pytest.raises(SlotTaken):
        async with storage.transaction() as conn:
            await conn.execute("INSERT INTO notes (title) VALUES (?)", ("Черновик",))
            raise SlotTaken

    assert await storage.fetch_value("SELECT COUNT(*) FROM notes") == 0


async def test_storage_works_again_after_a_failed_transaction(storage: DemoStorage):
    with pytest.raises(ValueError):
        async with storage.transaction():
            raise ValueError

    # Блокировка должна быть отпущена, а соединение — пригодно к работе.
    await storage.execute("INSERT INTO notes (title) VALUES (?)", ("После сбоя",))
    assert await storage.fetch_value("SELECT COUNT(*) FROM notes") == 1


async def test_settings_are_stored_and_read(storage: DemoStorage):
    assert await storage.get_setting("last_poll") is None
    assert await storage.get_setting("last_poll", "никогда") == "никогда"

    await storage.set_setting("last_poll", "2026-07-28")
    await storage.set_setting("last_poll", "2026-07-29")
    assert await storage.get_setting("last_poll") == "2026-07-29"


async def test_flags(storage: DemoStorage):
    assert await storage.get_flag("paused") is False
    await storage.set_flag("paused", True)
    assert await storage.get_flag("paused") is True


async def test_cyrillic_search_works(storage: DemoStorage):
    """Встроенный lower() в SQLite не знает кириллицы — поэтому свой rulower."""
    await storage.execute("INSERT INTO notes (title) VALUES (?)", ("Чехол Силиконовый",))

    found = await storage.fetch_all("SELECT * FROM notes WHERE rulower(title) LIKE ?", ("%чехол%",))
    assert len(found) == 1
    assert rulower("ЧЕХОЛ") == "чехол"
    assert rulower(None) == ""


async def test_on_open_hook_runs(tmp_path: Path):
    class Migrating(DemoStorage):
        opened = 0

        async def on_open(self) -> None:
            type(self).opened += 1

    async with Migrating(str(tmp_path / "hook.db")):
        pass
    assert Migrating.opened == 1


def test_only_aware_datetimes_go_into_the_database():
    with pytest.raises(ValueError):
        to_iso(datetime(2026, 7, 28, 12))

    moment = datetime(2026, 7, 28, 12, tzinfo=UTC)
    assert from_iso(to_iso(moment)) == moment
    assert from_iso(None) is None


def test_default_db_path_is_relative():
    assert BotSettings(bot_token="x").db_path == "data/bot.db"
