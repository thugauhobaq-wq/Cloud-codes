"""Тексты бота: то, что читает участник и организатор.

Проверяем не формулировки, а свойства: разметка не ломается о чужие имена,
объявление содержит всё нужное для проверки, а числа согласованы со словами.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from conftest import NOW
from giveaway.config import Settings
from giveaway.draw import make_seed, pick_winners
from giveaway.models import STATUS_CANCELLED, STATUS_FINISHED, Giveaway, Participant, Winner
from giveaway.texts import (
    announcement,
    fmt_deadline,
    giveaway_card,
    giveaway_line,
    human_left,
    join_confirmation,
    mention,
    post_for_channel,
    proof,
    subscription_gate,
    winner_message,
)


@pytest.fixture
def giveaway() -> Giveaway:
    return Giveaway(
        id=12,
        title="Кофе в подарок",
        prize="Пачка зёрен 250 г",
        winners_count=2,
        ends_at=NOW + timedelta(days=3),
        channels=["@mychannel"],
        participants=42,
    )


# ── сроки ─────────────────────────────────────────────────────────────────


def test_deadline_is_written_in_russian():
    assert fmt_deadline(datetime(2026, 9, 5, 18, 30, tzinfo=UTC)) == "5 сентября, 18:30 UTC"


def test_a_giveaway_without_a_deadline_says_so():
    assert "организатор" in fmt_deadline(None)


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(days=2), "2 дня"),
        (timedelta(days=1), "1 день"),
        (timedelta(days=5), "5 дней"),
        (timedelta(hours=3), "3 часа"),
        (timedelta(hours=1), "1 час"),
        (timedelta(minutes=12), "12 минут"),
        (timedelta(minutes=1), "1 минута"),
    ],
)
def test_time_left_agrees_with_the_number(delta: timedelta, expected: str):
    assert human_left(delta) == expected


def test_less_than_a_minute_is_not_zero_minutes():
    assert human_left(timedelta(seconds=20)) == "1 минута"


def test_a_deadline_that_has_passed_reads_as_about_to_happen():
    assert human_left(timedelta(seconds=-5)) == "вот-вот"


# ── карточка ──────────────────────────────────────────────────────────────


def test_card_shows_what_a_participant_needs(giveaway: Giveaway, settings: Settings):
    text = giveaway_card(giveaway, settings)

    assert giveaway.title in text
    assert giveaway.prize in text
    assert "Победителей: 2" in text
    assert "Участников: 42" in text
    assert "@mychannel" in text


def test_card_hides_the_seed_from_participants(settings: Settings, giveaway: Giveaway):
    """Зерно до итогов не существует, а номер и ссылка — служебное."""
    giveaway.seed = "abc123"

    assert "abc123" not in giveaway_card(giveaway, settings)
    assert "abc123" in giveaway_card(giveaway, settings, for_admin=True)


def test_a_finished_card_shows_the_status_instead_of_the_countdown(
    giveaway: Giveaway, settings: Settings
):
    giveaway.status = STATUS_FINISHED

    text = giveaway_card(giveaway, settings)

    assert "завершён" in text
    assert "Осталось" not in text


def test_the_admin_card_carries_the_link(giveaway: Giveaway, settings: Settings):
    assert "t.me/giveaway_bot?start=g12" in giveaway_card(giveaway, settings, for_admin=True)


def test_a_link_without_a_username_says_what_to_fix(giveaway: Giveaway):
    """Молчаливо неверная ссылка хуже, чем прямое указание на .env."""
    settings = Settings(bot_token="test", owner_id=1, bot_username="")

    assert "BOT_USERNAME" in settings.start_link(giveaway.id)


def test_the_list_line_agrees_with_the_participant_count(giveaway: Giveaway):
    assert "42 участника" in giveaway_line(giveaway)
    assert "🟢" in giveaway_line(giveaway)


def test_a_long_title_is_cut_in_the_list():
    long = Giveaway(id=1, title="Очень длинное название розыгрыша, " * 5)

    assert len(giveaway_line(long)) < 120


def test_a_cancelled_giveaway_is_marked_in_the_list(giveaway: Giveaway):
    giveaway.status = STATUS_CANCELLED

    assert "отменён" in giveaway_line(giveaway)


# ── участие ───────────────────────────────────────────────────────────────


def test_confirmation_shows_the_number(giveaway: Giveaway):
    text = join_confirmation(giveaway, Participant(giveaway_id=12, tg_id=100, number=7))

    assert "Ваш номер: 7" in text
    assert giveaway.title in text


def test_the_gate_names_the_channels_that_are_missing(giveaway: Giveaway):
    text = subscription_gate(giveaway, ["@one", "@two"])

    assert "@one" in text and "@two" in text
    assert "Я подписался" in text


# ── итоги ─────────────────────────────────────────────────────────────────


def make_winners(participants: list[int], seed: str, count: int = 2) -> list[Winner]:
    return [
        Winner(giveaway_id=12, tg_id=tg_id, place=place, seed=seed)
        for place, tg_id in enumerate(pick_winners(participants, count, seed), 1)
    ]


def test_the_announcement_carries_everything_needed_to_check_it(
    giveaway: Giveaway, settings: Settings
):
    participants = list(range(100, 120))
    giveaway.seed = make_seed(giveaway.id, NOW, participants)
    winners = make_winners(participants, giveaway.seed)

    text = announcement(giveaway, winners, settings)

    assert giveaway.seed in text
    assert "sha256" in text
    assert "Участников: 42" in text
    assert all(str(item.place) in text for item in winners)


def test_a_winner_with_a_username_is_mentioned_by_it():
    assert mention(Winner(giveaway_id=12, tg_id=100, place=1, username="anya")) == "@anya"


def test_a_winner_without_a_username_is_linked_by_id():
    text = mention(Winner(giveaway_id=12, tg_id=100, place=1, name="Аня"))

    assert 'href="tg://user?id=100"' in text
    assert "Аня" in text


def test_a_nameless_winner_still_gets_a_link():
    assert "tg://user?id=100" in mention(Winner(giveaway_id=12, tg_id=100, place=1))


def test_html_in_a_name_cannot_break_the_message():
    """Имя приходит от пользователя: «<b»  в нём не должно ломать разметку."""
    text = mention(Winner(giveaway_id=12, tg_id=100, place=1, name="<b>жирный</b>"))

    assert "<b>жирный</b>" not in text
    assert "&lt;b&gt;" in text


def test_html_in_a_title_is_escaped(settings: Settings):
    giveaway = Giveaway(id=1, title="<script>alert(1)</script>")

    assert "<script>" not in giveaway_card(giveaway, settings)


def test_the_winner_message_says_how_long_they_have(giveaway: Giveaway, settings: Settings):
    text = winner_message(giveaway, Winner(giveaway_id=12, tg_id=100, place=1), settings)

    assert "Вы выиграли" in text
    assert giveaway.prize in text
    assert str(settings.claim_hours) in text


def test_a_giveaway_without_a_prize_falls_back_to_its_title(settings: Settings):
    giveaway = Giveaway(id=1, title="Кофе")

    assert "Кофе" in winner_message(giveaway, Winner(giveaway_id=1, tg_id=1, place=1), settings)


def test_the_proof_marks_the_winners(giveaway: Giveaway):
    participants = list(range(100, 130))
    giveaway.seed = make_seed(giveaway.id, NOW, participants)
    winners = make_winners(participants, giveaway.seed)

    text = proof(giveaway, participants, winners)

    assert giveaway.seed in text
    assert text.count("← победитель") == len(winners)
    assert "Участников: 30" in text


# ── пост для канала ───────────────────────────────────────────────────────


def test_the_channel_post_is_ready_to_copy(giveaway: Giveaway, settings: Settings):
    text = post_for_channel(giveaway, settings)

    assert giveaway.title in text
    assert giveaway.prize in text
    assert "Победителей: 2" in text
    assert "@mychannel" in text
    assert "t.me/giveaway_bot?start=g12" in text
