from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from giveaway.draw import SEED_LENGTH, make_seed, pick_winners, reroll_seed, ticket, verify

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
PEOPLE = list(range(1000, 1050))


def test_seed_is_short_enough_for_a_message():
    assert len(make_seed(1, NOW, PEOPLE)) == SEED_LENGTH


def test_same_input_gives_the_same_seed():
    assert make_seed(1, NOW, PEOPLE) == make_seed(1, NOW, PEOPLE)


def test_participant_order_does_not_change_the_seed():
    """Порядок строк в базе не должен влиять на жребий."""
    assert make_seed(1, NOW, PEOPLE) == make_seed(1, NOW, list(reversed(PEOPLE)))


def test_seed_changes_with_the_participants():
    """Если состав подменили после объявления, зерно перестаёт сходиться."""
    assert make_seed(1, NOW, PEOPLE) != make_seed(1, NOW, [*PEOPLE, 9999])


def test_seed_changes_with_the_moment():
    later = NOW + timedelta(seconds=1)
    assert make_seed(1, NOW, PEOPLE) != make_seed(1, later, PEOPLE)


def test_seed_changes_with_the_giveaway():
    assert make_seed(1, NOW, PEOPLE) != make_seed(2, NOW, PEOPLE)


# ── выбор победителей ─────────────────────────────────────────────────────


def test_draw_is_repeatable():
    seed = make_seed(1, NOW, PEOPLE)
    assert pick_winners(PEOPLE, 3, seed) == pick_winners(PEOPLE, 3, seed)


def test_winners_are_participants_and_unique():
    winners = pick_winners(PEOPLE, 5, make_seed(1, NOW, PEOPLE))

    assert len(winners) == 5
    assert len(set(winners)) == 5
    assert set(winners) <= set(PEOPLE)


def test_winner_order_is_stable_when_asking_for_more():
    """Второе место остаётся вторым, сколько бы призов ни разыгрывали."""
    seed = make_seed(1, NOW, PEOPLE)
    assert pick_winners(PEOPLE, 5, seed)[:2] == pick_winners(PEOPLE, 2, seed)


def test_result_does_not_depend_on_the_order_of_the_list():
    seed = make_seed(1, NOW, PEOPLE)
    assert pick_winners(PEOPLE, 3, seed) == pick_winners(list(reversed(PEOPLE)), 3, seed)


def test_duplicates_in_the_list_do_not_double_the_chances():
    seed = make_seed(1, NOW, PEOPLE)
    assert pick_winners([*PEOPLE, *PEOPLE], 3, seed) == pick_winners(PEOPLE, 3, seed)


def test_more_prizes_than_people_gives_everyone():
    winners = pick_winners([1, 2], 5, "seed")
    assert sorted(winners) == [1, 2]


@pytest.mark.parametrize(("people", "count"), [([], 3), ([1, 2], 0), ([], 0)])
def test_nothing_to_draw(people, count):
    assert pick_winners(people, count, "seed") == []


def test_different_seeds_usually_give_different_winners():
    """Жребий должен зависеть от зерна, иначе он не жребий."""
    first = pick_winners(PEOPLE, 3, "seed-one")
    second = pick_winners(PEOPLE, 3, "seed-two")
    assert first != second


def test_draw_is_spread_over_participants():
    """За сто розыгрышей побеждать должны разные люди, а не одни и те же."""
    winners = {pick_winners(PEOPLE, 1, f"seed-{index}")[0] for index in range(100)}
    assert len(winners) > 20


# ── проверка результата ───────────────────────────────────────────────────


def test_verify_accepts_an_honest_result():
    seed = make_seed(1, NOW, PEOPLE)
    assert verify(PEOPLE, 3, seed, pick_winners(PEOPLE, 3, seed))


def test_verify_rejects_a_substituted_winner():
    """Ровно то, ради чего всё это: подмену победителя видно."""
    seed = make_seed(1, NOW, PEOPLE)
    winners = pick_winners(PEOPLE, 3, seed)
    tampered = [9999, *winners[1:]]

    assert not verify(PEOPLE, 3, seed, tampered)


def test_verify_rejects_a_reordered_result():
    seed = make_seed(1, NOW, PEOPLE)
    winners = pick_winners(PEOPLE, 3, seed)
    assert not verify(PEOPLE, 3, seed, list(reversed(winners)))


def test_ticket_is_deterministic_and_looks_random():
    assert ticket("seed", 1) == ticket("seed", 1)
    assert ticket("seed", 1) != ticket("seed", 2)
    assert ticket("other", 1) != ticket("seed", 1)


# ── замена победителя ─────────────────────────────────────────────────────


def test_reroll_seed_is_derived_not_random():
    assert reroll_seed("seed", 1) == reroll_seed("seed", 1)
    assert reroll_seed("seed", 1) != reroll_seed("seed", 2)
    assert reroll_seed("seed", 1) != "seed"
