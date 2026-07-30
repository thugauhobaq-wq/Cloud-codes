"""Форматирование сообщений: единственное место, где UTC превращается в местное время."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from html import escape as _html_escape
from zoneinfo import ZoneInfo

from .models import WEEKDAY_NAMES, Booking, Service

MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def escape(text: str) -> str:
    return _html_escape(text or "", quote=False)


def local(moment: datetime, tz: ZoneInfo) -> datetime:
    return moment.astimezone(tz)


def fmt_time(moment: datetime, tz: ZoneInfo) -> str:
    return local(moment, tz).strftime("%H:%M")


def fmt_date(day: date) -> str:
    return f"{day.day} {MONTHS[day.month - 1]}"


def fmt_day_label(day: date, today: date) -> str:
    """Подпись дня для календаря: «сегодня», «завтра» или «пн, 3 авг»."""
    delta = (day - today).days
    if delta == 0:
        return "сегодня"
    if delta == 1:
        return "завтра"
    return f"{WEEKDAY_NAMES[day.weekday()]}, {day.day} {MONTHS[day.month - 1][:3]}"


def fmt_moment(moment: datetime, tz: ZoneInfo, *, today: date | None = None) -> str:
    """«завтра в 14:00» или «3 августа (пн) в 14:00»."""
    point = local(moment, tz)
    today = today or datetime.now(tz).date()
    delta = (point.date() - today).days
    if delta == 0:
        prefix = "сегодня"
    elif delta == 1:
        prefix = "завтра"
    else:
        prefix = f"{fmt_date(point.date())} ({WEEKDAY_NAMES[point.weekday()]})"
    return f"{prefix} в {point:%H:%M}"


def human_duration(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours} ч {rest} мин"
    if hours:
        return f"{hours} ч"
    return f"{rest} мин"


def human_left(delta: timedelta) -> str:
    """Сколько осталось до записи — для напоминания."""
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} мин"
    return human_duration(minutes)


def fmt_price(price: int | None) -> str:
    if not price:
        return "по договорённости"
    return f"{price:,} ₽".replace(",", " ")


def service_line(service: Service) -> str:
    parts = [f"<b>{escape(service.title)}</b>", human_duration(service.duration_min)]
    if service.price:
        parts.append(fmt_price(service.price))
    line = " · ".join(parts)
    if service.description:
        line += f"\n<i>{escape(service.description)}</i>"
    return line


def services_list(services: list[Service]) -> str:
    if not services:
        return "Услуги пока не заведены."
    return "\n\n".join(f"• {service_line(item)}" for item in services)


def booking_card(booking: Booking, tz: ZoneInfo, *, with_client: bool = False) -> str:
    """Карточка записи. Для админа добавляем клиента и телефон."""
    lines = [
        f"<b>{escape(booking.service_title)}</b>",
        f"🗓 {fmt_moment(booking.starts_at, tz)} – {fmt_time(booking.ends_at, tz)}",
    ]
    if booking.master_name:
        lines.append(f"💇 {escape(booking.master_name)}")
    if with_client:
        who = escape(booking.client_name or "без имени")
        lines.append(f"👤 {who}")
        if booking.client_phone:
            lines.append(f"📞 {escape(booking.client_phone)}")
    if booking.comment:
        lines.append(f"💬 {escape(booking.comment)}")
    return "\n".join(lines)


def admin_booking_line(booking: Booking, tz: ZoneInfo) -> str:
    """Одна строка расписания на день — компактно, чтобы влезал весь день."""
    who = escape(booking.client_name or "без имени")
    master = f" · {escape(booking.master_name)}" if booking.master_name else ""
    phone = f" · {escape(booking.client_phone)}" if booking.client_phone else ""
    return (
        f"<b>{fmt_time(booking.starts_at, tz)}–{fmt_time(booking.ends_at, tz)}</b> "
        f"{escape(booking.service_title)}{master}\n    {who}{phone}"
    )


def day_schedule(bookings: list[Booking], tz: ZoneInfo, title: str) -> str:
    if not bookings:
        return f"<b>{escape(title)}</b>\n\nЗаписей нет."
    lines = [f"<b>{escape(title)}</b>", ""]
    lines.extend(admin_booking_line(item, tz) for item in bookings)
    return "\n".join(lines)
