"""Подведение итогов по сроку и рассылка результатов.

Проверяется поведение, которое видит организатор: розыгрыш закрывается сам,
объявление доходит хотя бы до кого-то, а победители, заблокировавшие бота,
не теряются молча.
"""

from __future__ import annotations

from datetime import timedelta

from botkit.testing import FakeBot

from conftest import NOW, fill
from giveaway.config import Settings
from giveaway.models import STATUS_CANCELLED, STATUS_FINISHED
from giveaway.notify import Notify
from giveaway.storage import Storage
from giveaway.workers import Deadlines


def worker(storage: Storage, notify: Notify, settings: Settings) -> Deadlines:
    return Deadlines(storage, notify, settings)


async def test_expired_giveaway_is_finished(storage: Storage, notify: Notify, settings: Settings):
    giveaway = await storage.create_giveaway("Кофе", ends_at=NOW - timedelta(minutes=1))
    await fill(storage, giveaway.id, 5)

    assert await worker(storage, notify, settings).tick() == 1

    fresh = await storage.get_giveaway(giveaway.id)
    assert fresh is not None and fresh.status == STATUS_FINISHED
    assert len(await storage.winners(giveaway.id)) == 1


async def test_a_running_giveaway_is_left_alone(
    storage: Storage, notify: Notify, settings: Settings
):
    giveaway = await storage.create_giveaway("Кофе", ends_at=NOW + timedelta(days=1))
    await fill(storage, giveaway.id, 5)

    assert await worker(storage, notify, settings).tick() == 0

    fresh = await storage.get_giveaway(giveaway.id)
    assert fresh is not None and fresh.is_active


async def test_a_giveaway_without_a_deadline_waits_for_the_organiser(
    storage: Storage, notify: Notify, settings: Settings
):
    giveaway = await storage.create_giveaway("Кофе")
    await fill(storage, giveaway.id, 5)

    assert await worker(storage, notify, settings).tick() == 0
    fresh = await storage.get_giveaway(giveaway.id)
    assert fresh is not None and fresh.is_active


async def test_winners_are_told_personally(
    storage: Storage, notify: Notify, settings: Settings, bot: FakeBot
):
    giveaway = await storage.create_giveaway(
        "Кофе", prize="Зёрна", winners_count=2, ends_at=NOW - timedelta(minutes=1)
    )
    await fill(storage, giveaway.id, 8)

    await worker(storage, notify, settings).tick()

    winners = await storage.winners(giveaway.id)
    for winner in winners:
        assert any("Вы выиграли" in text for text in bot.to(winner.tg_id))
    assert all(item.notified for item in winners)


async def test_the_announcement_goes_to_the_channel(
    storage: Storage, settings: Settings, bot: FakeBot
):
    settings.announce_chat = "@results"
    notify = Notify(bot, settings.admins(), settings)
    giveaway = await storage.create_giveaway("Кофе", ends_at=NOW - timedelta(minutes=1))
    await fill(storage, giveaway.id, 5)

    await worker(storage, notify, settings).tick()

    published = bot.to("@results")
    assert published and "Итоги розыгрыша" in published[0]
    # Зерно публикуется вместе с итогами — без него проверить нечего.
    fresh = await storage.get_giveaway(giveaway.id)
    assert fresh is not None and fresh.seed in published[0]


async def test_without_a_channel_the_announcement_goes_to_the_owner(
    storage: Storage, notify: Notify, settings: Settings, bot: FakeBot
):
    """Объявление не должно потеряться из-за ненастроенного канала."""
    giveaway = await storage.create_giveaway("Кофе", ends_at=NOW - timedelta(minutes=1))
    await fill(storage, giveaway.id, 5)

    await worker(storage, notify, settings).tick()

    assert any("Итоги розыгрыша" in text for text in bot.to(settings.owner_id))


async def test_a_giveaway_nobody_joined_is_closed_and_reported(
    storage: Storage, notify: Notify, settings: Settings, bot: FakeBot
):
    """Иначе воркер будет пытаться разыграть его каждую минуту, вечно."""
    giveaway = await storage.create_giveaway("Пустой", ends_at=NOW - timedelta(minutes=1))

    assert await worker(storage, notify, settings).tick() == 0

    fresh = await storage.get_giveaway(giveaway.id)
    assert fresh is not None and fresh.status == STATUS_CANCELLED
    assert any("без участников" in text for text in bot.to(settings.owner_id))
    assert await worker(storage, notify, settings).tick() == 0


async def test_blocked_winners_are_reported_to_the_owner(
    storage: Storage, settings: Settings, bot: FakeBot
):
    """Победитель, закрывший личку, — повод для перевыбора, а не тишины."""
    giveaway = await storage.create_giveaway("Кофе", ends_at=NOW - timedelta(minutes=1))
    await fill(storage, giveaway.id, 5)

    # Кого выберет жребий, заранее неизвестно: блокируем всех участников,
    # чтобы попасть в победителя, кем бы он ни оказался.
    bot.unreachable = set(range(100, 105))
    notify = Notify(bot, settings.admins(), settings)

    await worker(storage, notify, settings).tick()

    assert any("не удалось написать" in text for text in bot.to(settings.owner_id))
    winners = await storage.winners(giveaway.id)
    assert winners and not any(item.notified for item in winners)


async def test_several_giveaways_are_finished_in_one_tick(
    storage: Storage, notify: Notify, settings: Settings
):
    for title in ("Кофе", "Чай", "Какао"):
        giveaway = await storage.create_giveaway(title, ends_at=NOW - timedelta(minutes=1))
        await fill(storage, giveaway.id, 4, start=1000 * giveaway.id)

    assert await worker(storage, notify, settings).tick() == 3


async def test_a_tick_after_everything_is_finished_does_nothing(
    storage: Storage, notify: Notify, settings: Settings, bot: FakeBot
):
    """Перезапуск контейнера не должен рассылать итоги повторно."""
    giveaway = await storage.create_giveaway("Кофе", ends_at=NOW - timedelta(minutes=1))
    await fill(storage, giveaway.id, 5)
    await worker(storage, notify, settings).tick()
    sent = len(bot.messages)

    assert await worker(storage, notify, settings).tick() == 0
    assert len(bot.messages) == sent
