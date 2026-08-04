from __future__ import annotations

from conftest import add_magnet
from leadmagnet.broadcast import BLOCKED, FAILED, SENT, Broadcaster, Progress
from leadmagnet.config import Settings
from leadmagnet.storage import Storage


class FakeSender:
    """Подставная отправка: помнит, кому писали, и умеет отказывать."""

    def __init__(self, blocked: set[int] | None = None, failing: set[int] | None = None) -> None:
        self.blocked = blocked or set()
        self.failing = failing or set()
        self.sent: list[tuple[int, str]] = []

    async def __call__(self, chat_id: int, text: str) -> str:
        if chat_id in self.blocked:
            return BLOCKED
        if chat_id in self.failing:
            return FAILED
        self.sent.append((chat_id, text))
        return SENT


async def fill(storage: Storage, count: int = 3) -> None:
    for tg_id in range(1, count + 1):
        await storage.upsert_lead(tg_id, name=f"Подписчик {tg_id}")


async def test_broadcast_reaches_everyone(storage: Storage, settings: Settings):
    await fill(storage, 3)
    sender = FakeSender()

    record = await Broadcaster(storage, settings, sender).run("Привет!")

    assert [chat_id for chat_id, _ in sender.sent] == [1, 2, 3]
    assert record.total == 3
    assert record.sent == 3
    assert record.failed == 0
    assert record.status == "done"


async def test_blocked_users_are_marked_and_skipped_next_time(
    storage: Storage, settings: Settings
):
    await fill(storage, 3)
    broadcaster = Broadcaster(storage, settings, FakeSender(blocked={2}))

    first = await broadcaster.run("Первое")
    assert first.sent == 2
    assert first.failed == 1
    assert (await storage.get_lead(2)).blocked

    # Второй раз заблокировавший в выборку уже не попадает.
    second = await Broadcaster(storage, settings, FakeSender()).run("Второе")
    assert second.total == 2


async def test_temporary_failure_does_not_mark_the_lead(storage: Storage, settings: Settings):
    """Сетевой сбой — не повод вычёркивать человека из базы навсегда."""
    await fill(storage, 2)
    record = await Broadcaster(storage, settings, FakeSender(failing={1})).run("Текст")

    assert record.sent == 1
    assert record.failed == 1
    assert not (await storage.get_lead(1)).blocked


async def test_segmented_broadcast_reaches_only_the_segment(
    storage: Storage, settings: Settings
):
    checklist = await add_magnet(storage, "чеклист")
    await fill(storage, 3)
    await storage.record_delivery(1, checklist.id)
    await storage.record_delivery(3, checklist.id)

    sender = FakeSender()
    record = await Broadcaster(storage, settings, sender).run(
        "Только для получивших чек-лист", magnet_code="чеклист"
    )

    assert [chat_id for chat_id, _ in sender.sent] == [1, 3]
    assert record.magnet_code == "чеклист"


async def test_empty_audience_produces_an_empty_record(storage: Storage, settings: Settings):
    record = await Broadcaster(storage, settings, FakeSender()).run("Никому")
    assert record.total == 0
    assert record.sent == 0


async def test_progress_is_reported(storage: Storage, settings: Settings):
    await fill(storage, 4)
    seen: list[Progress] = []

    async def on_progress(progress: Progress) -> None:
        seen.append(Progress(progress.total, progress.sent, progress.failed, progress.blocked))

    await Broadcaster(storage, settings, FakeSender()).run(
        "Текст", on_progress=on_progress, progress_every=2
    )

    # Дважды по ходу (после 2 и 4) и один раз в конце.
    assert [item.sent for item in seen] == [2, 4, 4]


async def test_broadcast_history_is_kept(storage: Storage, settings: Settings):
    await fill(storage, 1)
    broadcaster = Broadcaster(storage, settings, FakeSender())
    await broadcaster.run("Первая")
    await broadcaster.run("Вторая")

    history = await storage.last_broadcasts()
    assert [item.text for item in history] == ["Вторая", "Первая"]
