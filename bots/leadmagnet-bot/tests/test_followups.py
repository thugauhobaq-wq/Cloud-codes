from __future__ import annotations

from datetime import UTC, datetime, timedelta

from conftest import add_magnet
from leadmagnet.config import Settings
from leadmagnet.followups import FollowUps
from leadmagnet.storage import Storage
from test_broadcast import FakeSender


async def prepare(storage: Storage, followup: str = "Ну как чек-лист?"):
    magnet = await add_magnet(storage, followup=followup)
    await storage.upsert_lead(1, name="Иван")
    delivery = await storage.record_delivery(1, magnet.id)
    return magnet, delivery


async def test_followup_goes_out_after_the_delay(storage: Storage, settings: Settings):
    await prepare(storage)
    sender = FakeSender()
    later = datetime.now(UTC) + timedelta(hours=25)

    assert await FollowUps(storage, settings, sender).tick(later) == 1
    assert sender.sent == [(1, "Ну как чек-лист?")]


async def test_nothing_happens_before_the_delay(storage: Storage, settings: Settings):
    await prepare(storage)
    sender = FakeSender()

    assert await FollowUps(storage, settings, sender).tick(datetime.now(UTC)) == 0
    assert sender.sent == []


async def test_followup_is_not_repeated(storage: Storage, settings: Settings):
    await prepare(storage)
    followups = FollowUps(storage, settings, FakeSender())
    later = datetime.now(UTC) + timedelta(hours=25)

    await followups.tick(later)
    assert await followups.tick(later + timedelta(hours=1)) == 0


async def test_blocked_lead_is_marked_and_not_retried(storage: Storage, settings: Settings):
    await prepare(storage)
    followups = FollowUps(storage, settings, FakeSender(blocked={1}))
    later = datetime.now(UTC) + timedelta(hours=25)

    assert await followups.tick(later) == 0
    assert (await storage.get_lead(1)).blocked
    # Отметку ставим в любом случае — иначе попытка повторялась бы каждые пять минут.
    assert await followups.tick(later) == 0


async def test_disabled_followups_do_nothing(storage: Storage, settings: Settings):
    settings.followup_enabled = False
    await prepare(storage)
    sender = FakeSender()

    assert await FollowUps(storage, settings, sender).tick(
        datetime.now(UTC) + timedelta(days=5)
    ) == 0
    assert sender.sent == []


async def test_magnet_without_followup_is_skipped(storage: Storage, settings: Settings):
    await prepare(storage, followup="")
    sender = FakeSender()

    assert await FollowUps(storage, settings, sender).tick(
        datetime.now(UTC) + timedelta(days=5)
    ) == 0
