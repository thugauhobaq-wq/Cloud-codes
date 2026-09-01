"""База: набор участников, подведение итогов, перевыбор.

Главное свойство, которое здесь защищается, — итоги остаются проверяемыми.
Зерно считается по составу участников, поэтому всё, что меняет состав после
жеребьёвки, ломает проверку и должно быть запрещено на уровне базы.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import NOW, fill
from giveaway.draw import verify
from giveaway.models import STATUS_ACTIVE, STATUS_CANCELLED, STATUS_FINISHED
from giveaway.storage import AlreadyFinished, NoParticipants, Storage


async def test_giveaway_is_created_with_defaults(storage: Storage):
    giveaway = await storage.create_giveaway("Кофе")

    assert giveaway.id > 0
    assert giveaway.title == "Кофе"
    assert giveaway.winners_count == 1
    assert giveaway.status == STATUS_ACTIVE
    assert giveaway.participants == 0
    assert giveaway.seed == ""


async def test_channels_survive_the_round_trip(storage: Storage):
    created = await storage.create_giveaway("Кофе", channels=["@one", "@two"])

    loaded = await storage.get_giveaway(created.id)

    assert loaded is not None
    assert loaded.channels == ["@one", "@two"]


async def test_zero_winners_is_corrected_to_one(storage: Storage):
    """Розыгрыш без победителей бессмыслен — это опечатка, а не намерение."""
    giveaway = await storage.create_giveaway("Кофе", winners_count=0)

    assert giveaway.winners_count == 1


async def test_participants_get_consecutive_numbers(storage: Storage):
    giveaway = await storage.create_giveaway("Кофе")

    first, _ = await storage.join(giveaway.id, 100)
    second, _ = await storage.join(giveaway.id, 200)

    assert (first.number, second.number) == (1, 2)


async def test_joining_twice_returns_the_same_number(storage: Storage):
    """Повторное нажатие кнопки — не ошибка и не второй билет."""
    giveaway = await storage.create_giveaway("Кофе")
    await storage.join(giveaway.id, 100)

    participant, already = await storage.join(giveaway.id, 100)

    assert already is True
    assert participant.number == 1
    assert await storage.count_participants(giveaway.id) == 1


async def test_the_same_person_can_join_different_giveaways(storage: Storage):
    first = await storage.create_giveaway("Кофе")
    second = await storage.create_giveaway("Чай")

    await storage.join(first.id, 100)
    _, already = await storage.join(second.id, 100)

    assert already is False


async def test_participant_counter_is_visible_on_the_card(storage: Storage):
    giveaway = await storage.create_giveaway("Кофе")
    await fill(storage, giveaway.id, 3)

    fresh = await storage.get_giveaway(giveaway.id)

    assert fresh is not None and fresh.participants == 3


# ── итоги ─────────────────────────────────────────────────────────────────


async def test_finish_records_winners_and_a_seed(storage: Storage):
    giveaway = await storage.create_giveaway("Кофе", winners_count=2)
    await fill(storage, giveaway.id, 10)

    winners = await storage.finish(giveaway.id, NOW)

    assert [item.place for item in winners] == [1, 2]
    fresh = await storage.get_giveaway(giveaway.id)
    assert fresh is not None
    assert fresh.status == STATUS_FINISHED
    assert fresh.seed
    assert all(item.seed == fresh.seed for item in winners)


async def test_recorded_winners_pass_verification(storage: Storage):
    """Ради этого всё и затевалось: итоги пересчитываются по зерну."""
    giveaway = await storage.create_giveaway("Кофе", winners_count=3)
    await fill(storage, giveaway.id, 25)

    winners = await storage.finish(giveaway.id, NOW)
    fresh = await storage.get_giveaway(giveaway.id)

    assert fresh is not None
    assert verify(
        await storage.participant_ids(giveaway.id),
        fresh.winners_count,
        fresh.seed,
        [item.tg_id for item in winners],
    )


async def test_winners_keep_their_names(storage: Storage):
    giveaway = await storage.create_giveaway("Кофе")
    await storage.join(giveaway.id, 100, name="Аня", username="anya")

    winners = await storage.finish(giveaway.id, NOW)

    assert winners[0].name == "Аня"
    assert winners[0].username == "anya"


async def test_more_prizes_than_people_is_not_an_error(storage: Storage):
    """Пять призов и двое участников: выиграют оба, а не пятеро."""
    giveaway = await storage.create_giveaway("Кофе", winners_count=5)
    await fill(storage, giveaway.id, 2)

    winners = await storage.finish(giveaway.id, NOW)

    assert len(winners) == 2


async def test_finishing_twice_is_refused(storage: Storage):
    """Иначе одновременные /finish и срок дали бы два разных списка."""
    giveaway = await storage.create_giveaway("Кофе")
    await fill(storage, giveaway.id, 3)
    await storage.finish(giveaway.id, NOW)

    with pytest.raises(AlreadyFinished):
        await storage.finish(giveaway.id, NOW)


async def test_finishing_without_participants_is_refused(storage: Storage):
    giveaway = await storage.create_giveaway("Кофе")

    with pytest.raises(NoParticipants):
        await storage.finish(giveaway.id, NOW)

    fresh = await storage.get_giveaway(giveaway.id)
    assert fresh is not None and fresh.status == STATUS_ACTIVE


async def test_finishing_a_cancelled_giveaway_is_refused(storage: Storage):
    giveaway = await storage.create_giveaway("Кофе")
    await fill(storage, giveaway.id, 3)
    await storage.cancel_giveaway(giveaway.id)

    with pytest.raises(AlreadyFinished):
        await storage.finish(giveaway.id, NOW)


async def test_nobody_joins_after_the_draw(storage: Storage):
    """Опоздавший участник изменил бы состав — и проверка итогов сломалась бы."""
    giveaway = await storage.create_giveaway("Кофе")
    await fill(storage, giveaway.id, 3)
    winners = await storage.finish(giveaway.id, NOW)

    with pytest.raises(AlreadyFinished):
        await storage.join(giveaway.id, 999)

    fresh = await storage.get_giveaway(giveaway.id)
    assert fresh is not None
    assert verify(
        await storage.participant_ids(giveaway.id),
        fresh.winners_count,
        fresh.seed,
        [item.tg_id for item in winners],
    )


async def test_winners_of_a_participant_are_visible_to_them(storage: Storage):
    giveaway = await storage.create_giveaway("Кофе")
    await fill(storage, giveaway.id, 3)
    await storage.finish(giveaway.id, NOW)

    assert [item.id for item in await storage.giveaways_of(100)] == [giveaway.id]


# ── сроки ─────────────────────────────────────────────────────────────────


async def test_due_giveaways_are_those_whose_time_has_come(storage: Storage):
    past = await storage.create_giveaway("Прошёл", ends_at=NOW - timedelta(minutes=1))
    await storage.create_giveaway("Идёт", ends_at=NOW + timedelta(days=1))
    await storage.create_giveaway("Без срока")

    due = await storage.due_giveaways(NOW)

    assert [item.id for item in due] == [past.id]


async def test_finished_giveaway_leaves_the_queue(storage: Storage):
    giveaway = await storage.create_giveaway("Кофе", ends_at=NOW - timedelta(minutes=1))
    await fill(storage, giveaway.id, 2)
    await storage.finish(giveaway.id, NOW)

    assert await storage.due_giveaways(NOW) == []


async def test_cancel_only_works_once(storage: Storage):
    giveaway = await storage.create_giveaway("Кофе")

    assert (await storage.cancel_giveaway(giveaway.id)).status == STATUS_CANCELLED
    assert await storage.cancel_giveaway(giveaway.id) is None


# ── перевыбор ─────────────────────────────────────────────────────────────


async def test_reroll_replaces_a_silent_winner(storage: Storage):
    giveaway = await storage.create_giveaway("Кофе")
    await fill(storage, giveaway.id, 10)
    winners = await storage.finish(giveaway.id, NOW)
    was = winners[0].tg_id

    replacement = await storage.reroll(giveaway.id, place=1)

    assert replacement is not None
    assert replacement.tg_id != was
    assert replacement.round_number == 1
    assert replacement.seed != winners[0].seed


async def test_reroll_does_not_pick_another_winner(storage: Storage):
    giveaway = await storage.create_giveaway("Кофе", winners_count=3)
    await fill(storage, giveaway.id, 10)
    winners = await storage.finish(giveaway.id, NOW)
    others = {item.tg_id for item in winners if item.place != 1}

    replacement = await storage.reroll(giveaway.id, place=1)

    assert replacement is not None and replacement.tg_id not in others


async def test_reroll_is_reproducible(storage: Storage):
    """Замена проверяется так же, как основной жребий: зерно выводится из прежнего."""
    giveaway = await storage.create_giveaway("Кофе")
    await fill(storage, giveaway.id, 10)
    await storage.finish(giveaway.id, NOW)

    replacement = await storage.reroll(giveaway.id, place=1)

    assert replacement is not None
    everyone = await storage.participant_ids(giveaway.id)
    pool = [item for item in everyone if item != replacement.tg_id]
    assert verify([*pool, replacement.tg_id], 1, replacement.seed, [replacement.tg_id])


async def test_reroll_stops_when_everyone_has_won(storage: Storage):
    giveaway = await storage.create_giveaway("Кофе", winners_count=2)
    await fill(storage, giveaway.id, 2)
    await storage.finish(giveaway.id, NOW)

    assert await storage.reroll(giveaway.id, place=1) is None


async def test_reroll_needs_a_finished_giveaway(storage: Storage):
    giveaway = await storage.create_giveaway("Кофе")
    await fill(storage, giveaway.id, 5)

    assert await storage.reroll(giveaway.id, place=1) is None


async def test_reroll_resets_the_notification_mark(storage: Storage):
    giveaway = await storage.create_giveaway("Кофе")
    await fill(storage, giveaway.id, 10)
    winners = await storage.finish(giveaway.id, NOW)
    await storage.mark_notified(giveaway.id, winners[0].tg_id)

    replacement = await storage.reroll(giveaway.id, place=1)

    assert replacement is not None and replacement.notified is False


# ── выгрузка и сводка ─────────────────────────────────────────────────────


async def test_export_marks_the_winners(storage: Storage):
    giveaway = await storage.create_giveaway("Кофе")
    await fill(storage, giveaway.id, 5)
    winners = await storage.finish(giveaway.id, NOW)

    rows = await storage.export_rows(giveaway.id)

    assert len(rows) == 5
    places = {row[0]: row[4] for row in rows}
    assert places[winners[0].tg_id] == 1
    assert sum(1 for value in places.values() if value == "") == 4


async def test_stats_counts_people_once(storage: Storage):
    """Человек в трёх розыгрышах — три участия, но один подписчик."""
    first = await storage.create_giveaway("Кофе")
    second = await storage.create_giveaway("Чай")
    await storage.join(first.id, 100)
    await storage.join(second.id, 100)
    await storage.join(second.id, 200)

    stats = await storage.stats()

    assert stats["giveaways"] == 2
    assert stats["active"] == 2
    assert stats["participants"] == 3
    assert stats["people"] == 2


async def test_state_survives_a_restart(storage: Storage, settings):
    """База — не кэш: перезапуск контейнера не должен терять участников."""
    giveaway = await storage.create_giveaway("Кофе")
    await fill(storage, giveaway.id, 4)
    await storage.close()

    async with Storage(settings.db_path) as reopened:
        fresh = await reopened.get_giveaway(giveaway.id)
        assert fresh is not None and fresh.participants == 4

    await storage.open()  # фикстура закроет его сама
