"""Разбор команд организатора.

Он заводит розыгрыш с телефона, между делом, и пишет как придётся. Всё, что
разобрать нельзя, должно оборачиваться понятной фразой, а не трейсбеком.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from conftest import NOW
from giveaway.config import parse_channels
from giveaway.parsing import MAX_DAYS, ParseError, parse_deadline, parse_new

# ── срок ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "delta"),
    [
        ("3д", timedelta(days=3)),
        ("3 д", timedelta(days=3)),
        ("2 дня", timedelta(days=2)),
        ("10 дней", timedelta(days=10)),
        ("48ч", timedelta(hours=48)),
        ("6 часов", timedelta(hours=6)),
        ("90м", timedelta(minutes=90)),
        ("30 минут", timedelta(minutes=30)),
        ("2 недели", timedelta(weeks=2)),
    ],
)
def test_relative_deadlines(raw: str, delta: timedelta):
    assert parse_deadline(raw, NOW) == NOW + delta


@pytest.mark.parametrize("raw", ["3мес", "5 лет", "2 попугая"])
def test_unknown_units_do_not_turn_into_minutes(raw: str):
    """«3 мес» по началу слова совпало бы с «м» — и вышло бы три минуты."""
    with pytest.raises(ParseError, match="единицу"):
        parse_deadline(raw, NOW)


def test_a_unit_without_a_number_is_not_a_deadline():
    with pytest.raises(ParseError):
        parse_deadline("сутки", NOW)


def test_absolute_date_with_time():
    assert parse_deadline("2026-09-05 18:00", NOW) == datetime(2026, 9, 5, 18, tzinfo=UTC)


def test_russian_date_format():
    assert parse_deadline("5.09 18:00", NOW) == datetime(2026, 9, 5, 18, tzinfo=UTC)


def test_slashes_work_too():
    assert parse_deadline("5/09 18:00", NOW) == datetime(2026, 9, 5, 18, tzinfo=UTC)


def test_date_without_time_means_noon():
    """Полночь как «на 5 сентября» читается неверно: это конец 4-го."""
    assert parse_deadline("5.09", NOW) == datetime(2026, 9, 5, 12, tzinfo=UTC)


def test_time_without_date_is_the_next_occurrence():
    assert parse_deadline("18:00", NOW) == datetime(2026, 9, 1, 18, tzinfo=UTC)


def test_time_that_already_passed_today_means_tomorrow():
    assert parse_deadline("09:00", NOW) == datetime(2026, 9, 2, 9, tzinfo=UTC)


def test_short_date_in_the_past_means_next_year():
    """«5.01», написанное в декабре, — это январь, но следующий."""
    december = datetime(2026, 12, 20, 12, tzinfo=UTC)

    assert parse_deadline("5.01 18:00", december) == datetime(2027, 1, 5, 18, tzinfo=UTC)


def test_a_short_date_beyond_the_limit_is_refused_rather_than_moved():
    """«5.01» в сентябре — это либо опечатка, либо срок на четыре месяца."""
    with pytest.raises(ParseError, match=str(MAX_DAYS)):
        parse_deadline("5.01 18:00", NOW)


def test_year_can_be_two_digits():
    assert parse_deadline("5.10.26 18:00", NOW) == datetime(2026, 10, 5, 18, tzinfo=UTC)


@pytest.mark.parametrize("raw", ["", "   ", "завтра", "потом", "5.13 18:00", "18:70", "25:00"])
def test_unparseable_deadlines_explain_themselves(raw: str):
    with pytest.raises(ParseError) as exc:
        parse_deadline(raw, NOW)
    assert str(exc.value)
    assert "Traceback" not in str(exc.value)


def test_a_moment_in_the_past_is_refused():
    with pytest.raises(ParseError, match="прошёл"):
        parse_deadline("2020-01-01 10:00", NOW)


def test_too_far_away_is_refused():
    """«30д» вместо «30м» — типовая опечатка, и она дорогая."""
    with pytest.raises(ParseError, match=str(MAX_DAYS)):
        parse_deadline(f"{MAX_DAYS + 1}д", NOW)


def test_the_limit_itself_is_allowed():
    assert parse_deadline(f"{MAX_DAYS}д", NOW) == NOW + timedelta(days=MAX_DAYS)


# ── описание розыгрыша ────────────────────────────────────────────────────


def test_full_specification():
    spec = parse_new("Кофе | Пачка зёрен | 2 | 3д | @mychannel", NOW)

    assert spec.title == "Кофе"
    assert spec.prize == "Пачка зёрен"
    assert spec.winners_count == 2
    assert spec.ends_at == NOW + timedelta(days=3)
    assert spec.channels == ["@mychannel"]


def test_title_alone_is_enough():
    """Розыгрыш без срока подводится вручную — это нормальный сценарий."""
    spec = parse_new("Кофе", NOW)

    assert spec.title == "Кофе"
    assert spec.winners_count == 1
    assert spec.ends_at is None
    assert spec.channels == []


def test_empty_fields_are_skipped():
    spec = parse_new("Кофе |  |  | 3д", NOW)

    assert spec.prize == ""
    assert spec.winners_count == 1
    assert spec.ends_at == NOW + timedelta(days=3)


def test_winners_count_survives_a_word_next_to_it():
    assert parse_new("Кофе | приз | 3 победителя | 3д", NOW).winners_count == 3


def test_winners_count_is_at_least_one():
    assert parse_new("Кофе | приз | 0 | 3д", NOW).winners_count == 1


def test_several_channels_are_split():
    spec = parse_new("Кофе | приз | 1 | 3д | @one, @two", NOW)

    assert spec.channels == ["@one", "@two"]


@pytest.mark.parametrize("raw", ["", "   ", "|||"])
def test_missing_title_shows_an_example(raw: str):
    with pytest.raises(ParseError, match="/new"):
        parse_new(raw, NOW)


def test_unparseable_winners_count_is_reported():
    with pytest.raises(ParseError, match="победителей"):
        parse_new("Кофе | приз | много | 3д", NOW)


def test_a_bad_deadline_is_reported_as_a_deadline():
    with pytest.raises(ParseError, match="прошёл"):
        parse_new("Кофе | приз | 1 | 2020-01-01 10:00", NOW)


# ── каналы ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@one", ["@one"]),
        ("one", ["@one"]),
        ("t.me/one", ["@one"]),
        ("https://t.me/one", ["@one"]),
        ("https://t.me/one/", ["@one"]),
        ("@one,@two", ["@one", "@two"]),
        ("@one @two", ["@one", "@two"]),
        ("@one; @two", ["@one", "@two"]),
        ("-1001234567890", ["-1001234567890"]),
        ("", []),
        ("@one, @one", ["@one"]),
    ],
)
def test_channels_are_normalised(raw: str, expected: list[str]):
    assert parse_channels(raw) == expected
