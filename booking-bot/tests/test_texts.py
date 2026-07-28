from __future__ import annotations

from datetime import date, timedelta

from booking.models import Booking, Service
from booking.texts import (
    booking_card,
    escape,
    fmt_day_label,
    fmt_moment,
    fmt_price,
    human_duration,
    human_left,
    service_line,
)
from conftest import MSK, msk


def test_moment_uses_relative_prefix_for_the_next_two_days():
    today = date(2026, 7, 29)
    assert fmt_moment(msk(2026, 7, 29, 14), MSK, today=today) == "сегодня в 14:00"
    assert fmt_moment(msk(2026, 7, 30, 9, 30), MSK, today=today) == "завтра в 09:30"
    assert fmt_moment(msk(2026, 8, 3, 14), MSK, today=today) == "3 августа (Пн) в 14:00"


def test_moment_is_shown_in_local_time():
    # 09:00 UTC — это полдень в Москве, и клиенту нужен именно полдень.
    assert "12:00" in fmt_moment(msk(2026, 7, 30, 12), MSK, today=date(2026, 7, 29))


def test_day_label():
    today = date(2026, 7, 29)
    assert fmt_day_label(today, today) == "сегодня"
    assert fmt_day_label(date(2026, 7, 30), today) == "завтра"
    assert fmt_day_label(date(2026, 8, 3), today) == "Пн, 3 авг"


def test_duration_and_price():
    assert human_duration(90) == "1 ч 30 мин"
    assert human_duration(60) == "1 ч"
    assert human_duration(45) == "45 мин"
    assert fmt_price(1500) == "1 500 ₽"
    assert fmt_price(None) == "по договорённости"


def test_human_left():
    assert human_left(timedelta(minutes=45)) == "45 мин"
    assert human_left(timedelta(hours=2)) == "2 ч"
    assert human_left(timedelta(seconds=-10)) == "0 мин"


def test_service_line_includes_price_and_description():
    line = service_line(
        Service(id=1, title="Стрижка", duration_min=60, price=1500, description="с мытьём")
    )
    assert "Стрижка" in line
    assert "1 ч" in line
    assert "1 500 ₽" in line
    assert "с мытьём" in line


def test_html_is_escaped_in_user_data():
    """Имя вида «<b>Вася</b>» не должно ломать разметку сообщения."""
    booking = Booking(
        id=1,
        client_id=42,
        service_id=1,
        starts_at=msk(2026, 7, 30, 12),
        ends_at=msk(2026, 7, 30, 13),
        service_title="Стрижка",
        client_name="<b>Вася</b>",
    )
    card = booking_card(booking, MSK, with_client=True)
    assert "&lt;b&gt;Вася&lt;/b&gt;" in card
    assert "<b>Вася</b>" not in card


def test_escape_handles_empty():
    assert escape("") == ""
