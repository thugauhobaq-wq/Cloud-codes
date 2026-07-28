from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from booking.models import Interval, WorkWindow
from booking.schedule import (
    day_bounds_utc,
    day_slots,
    merge_intervals,
    parse_date,
    parse_schedule_spec,
    parse_time,
    parse_weekdays,
    parse_windows,
    windows_for,
)
from conftest import MSK, msk, workday

DAY = date(2026, 7, 30)  # четверг


def slots(**kwargs) -> list[str]:
    """Слоты в удобочитаемом виде: «10:00, 10:30, …» по местному времени."""
    kwargs.setdefault("windows", workday())
    kwargs.setdefault("tz", MSK)
    kwargs.setdefault("now", msk(2026, 7, 29, 9))
    kwargs.setdefault("duration_min", 60)
    return [item.astimezone(MSK).strftime("%H:%M") for item in day_slots(day=DAY, **kwargs)]


def test_slots_cover_the_window_with_the_given_step():
    result = slots(step_min=60)
    assert result[0] == "10:00"
    # Последний слот — 19:00: часовая услуга должна закончиться до 20:00.
    assert result[-1] == "19:00"
    assert len(result) == 10


def test_service_must_fit_into_the_window():
    # Двухчасовая услуга в окне 10:00–20:00 последний раз начинается в 18:00.
    assert slots(duration_min=120, step_min=60)[-1] == "18:00"


def test_long_service_does_not_fit_at_all():
    assert slots(duration_min=12 * 60) == []


def test_busy_interval_blocks_overlapping_slots():
    busy = [Interval(msk(2026, 7, 30, 12), msk(2026, 7, 30, 13))]
    result = slots(step_min=60, busy=busy)
    assert "12:00" not in result
    # Часовая услуга в 11:00 заканчивается ровно к началу занятого промежутка.
    assert "11:00" in result
    assert "13:00" in result


def test_busy_interval_blocks_a_slot_that_would_run_into_it():
    busy = [Interval(msk(2026, 7, 30, 12), msk(2026, 7, 30, 13))]
    # Полуторачасовая услуга с 11:00 доедет до 12:30 — в занятое время.
    assert "11:00" not in slots(duration_min=90, step_min=60, busy=busy)


def test_adjacent_bookings_do_not_block_each_other():
    busy = [Interval(msk(2026, 7, 30, 11), msk(2026, 7, 30, 12))]
    assert "12:00" in slots(step_min=60, busy=busy)


def test_two_windows_with_a_lunch_break():
    windows = [
        WorkWindow(weekday=DAY.weekday(), start_min=10 * 60, end_min=14 * 60),
        WorkWindow(weekday=DAY.weekday(), start_min=15 * 60, end_min=20 * 60),
    ]
    result = slots(windows=windows, step_min=60)
    assert "13:00" in result  # заканчивается ровно в 14:00
    assert "14:00" not in result
    assert "15:00" in result


def test_min_lead_hides_slots_that_are_too_soon():
    result = slots(now=msk(2026, 7, 30, 11, 30), min_lead_min=60, step_min=30)
    # Сейчас 11:30, запас — час: 12:00 и 12:30 уже поздно.
    assert result[0] == "12:30"


def test_past_day_has_no_slots():
    assert slots(now=msk(2026, 8, 1, 9)) == []


def test_other_weekday_schedule_is_ignored():
    monday_only = [WorkWindow(weekday=0, start_min=10 * 60, end_min=20 * 60)]
    assert slots(windows=monday_only) == []


def test_personal_schedule_overrides_the_common_one():
    common = WorkWindow(weekday=DAY.weekday(), start_min=10 * 60, end_min=20 * 60)
    personal = WorkWindow(weekday=DAY.weekday(), start_min=12 * 60, end_min=15 * 60, master_id=7)

    assert windows_for([common, personal], DAY, master_id=7) == [personal]
    assert windows_for([common, personal], DAY, master_id=8) == [common]
    assert windows_for([common, personal], DAY, master_id=None) == [common]


def test_zero_duration_yields_nothing():
    assert slots(duration_min=0) == []


def test_day_bounds_cover_exactly_one_local_day():
    start, end = day_bounds_utc(DAY, MSK)
    assert start == msk(2026, 7, 30, 0)
    assert end - start == timedelta(days=1)


def test_day_bounds_survive_the_dst_switch():
    # В Европе/Берлине 25 октября 2026 в сутках 25 часов — переход на зимнее время.
    from zoneinfo import ZoneInfo

    berlin = ZoneInfo("Europe/Berlin")
    start, end = day_bounds_utc(date(2026, 10, 25), berlin)
    assert end - start == timedelta(hours=25)


def test_merge_intervals_glues_overlaps():
    base = datetime(2026, 7, 30, 10, tzinfo=UTC)
    merged = merge_intervals(
        [
            Interval(base, base + timedelta(hours=2)),
            Interval(base + timedelta(hours=1), base + timedelta(hours=3)),
            Interval(base + timedelta(hours=5), base + timedelta(hours=6)),
        ]
    )
    assert len(merged) == 2
    assert merged[0].end == base + timedelta(hours=3)


# ── разбор пользовательского ввода ────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("10:00", 600), ("10", 600), ("9.30", 570), ("00:00", 0), ("24:00", 1440)],
)
def test_parse_time(raw: str, expected: int):
    assert parse_time(raw) == expected


@pytest.mark.parametrize("raw", ["25:00", "10:75", "вечером", ""])
def test_parse_time_rejects_nonsense(raw: str):
    with pytest.raises(ValueError):
        parse_time(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("пн-пт", [0, 1, 2, 3, 4]),
        ("сб,вс", [5, 6]),
        ("будни", [0, 1, 2, 3, 4]),
        ("все", [0, 1, 2, 3, 4, 5, 6]),
        ("вт", [1]),
        ("сб-вт", [0, 1, 5, 6]),  # диапазон через конец недели
    ],
)
def test_parse_weekdays(raw: str, expected: list[int]):
    assert parse_weekdays(raw) == expected


def test_parse_windows_with_a_break():
    assert parse_windows("10:00-14:00 15:00-20:00") == [(600, 840), (900, 1200)]


def test_parse_windows_day_off():
    assert parse_windows("выходной") == []


@pytest.mark.parametrize("raw", ["20:00-10:00", "10:00", "с утра до вечера"])
def test_parse_windows_rejects_nonsense(raw: str):
    with pytest.raises(ValueError):
        parse_windows(raw)


def test_parse_schedule_spec():
    assert parse_schedule_spec("пн-пт 10:00-20:00") == ([0, 1, 2, 3, 4], [(600, 1200)])


def test_parse_date_variants():
    today = date(2026, 7, 29)
    assert parse_date("сегодня", today) == today
    assert parse_date("завтра", today) == date(2026, 7, 30)
    assert parse_date("5.08", today) == date(2026, 8, 5)
    assert parse_date("2026-08-05", today) == date(2026, 8, 5)
    assert parse_date("05.08.2026", today) == date(2026, 8, 5)


def test_parse_date_without_year_jumps_to_the_next_one():
    # В конце декабря «5.01» — это про январь, а не про давно прошедшее число.
    assert parse_date("5.01", date(2026, 12, 20)) == date(2027, 1, 5)


def test_parse_date_rejects_nonexistent_day():
    with pytest.raises(ValueError):
        parse_date("31.02", date(2026, 7, 29))
