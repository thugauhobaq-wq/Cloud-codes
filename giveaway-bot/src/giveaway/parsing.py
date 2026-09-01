"""Разбор того, что организатор пишет в командах.

Он заводит розыгрыш с телефона между делом, поэтому срок можно указать и как
«3д», и как «48ч», и как «5.09 18:00». Ошибка разбора должна быть понятной:
организатор не программист и читать трейсбек не будет.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .config import parse_channels

#: Дольше месяца розыгрыши не живут: за это время про них забывают и сами
#: организаторы. Ограничение спасает от опечатки вроде «30д» вместо «30м».
MAX_DAYS = 60

# Число и слово: «3д», «10 дней», «48ч». Какие именно слова считаются
# единицами, решает _UNITS — регулярное выражение ловит только форму.
_RELATIVE_RE = re.compile(r"^(\d+)([а-яё]+)$")

_UNITS = {
    "м": "minutes", "мин": "minutes", "минут": "minutes", "минута": "minutes",
    "минуты": "minutes", "минуту": "minutes",
    "ч": "hours", "час": "hours", "часа": "hours", "часов": "hours", "часы": "hours",
    "д": "days", "дн": "days", "день": "days", "дня": "days", "дней": "days",
    "сут": "days", "сутки": "days", "суток": "days",
    "н": "weeks", "нед": "weeks", "неделя": "weeks", "недели": "weeks",
    "недель": "weeks", "неделю": "weeks",
}  # fmt: skip


class ParseError(ValueError):
    """Понятное сообщение для организатора."""


def parse_deadline(raw: str, now: datetime | None = None) -> datetime:
    """«3д», «48ч», «2026-09-05 18:00», «5.09 18:00» → момент в UTC."""
    now = now or datetime.now(UTC)
    text = (raw or "").strip().lower()
    if not text:
        raise ParseError("Не указан срок. Например: 3д, 48ч, 5.09 18:00")

    relative = _parse_relative(text, now)
    moment = relative if relative is not None else _parse_absolute(text, now)

    if moment <= now:
        raise ParseError("Этот момент уже прошёл — укажите будущий срок")
    if moment - now > timedelta(days=MAX_DAYS):
        raise ParseError(f"Слишком далеко: розыгрыш не может идти дольше {MAX_DAYS} дней")
    return moment


def _parse_relative(text: str, now: datetime) -> datetime | None:
    """«3д» → момент. `None` — это вообще не относительный срок.

    Единицы сверяются по списку целиком, а не по началу слова: иначе «3 мес»
    молча стало бы тремя минутами, и розыгрыш закончился бы в тот же вечер.
    """
    match = _RELATIVE_RE.match(text.replace(" ", ""))
    if not match:
        return None

    unit = _UNITS.get(match.group(2))
    if unit is None:
        raise ParseError(
            f"Не понял единицу «{match.group(2)}». "
            "Можно так: 30м, 6ч, 3д, 2 недели, или дату: 5.09 18:00"
        )
    return now + timedelta(**{unit: int(match.group(1))})


def _parse_absolute(text: str, now: datetime) -> datetime:
    """Дата и время. Время без даты — ближайшее наступление этого времени."""
    parts = text.split()
    date_part, time_part = (parts[0], parts[1]) if len(parts) > 1 else (parts[0], "")

    if ":" in date_part and not time_part:
        date_part, time_part = "", date_part

    hour, minute = _parse_time(time_part) if time_part else (12, 0)
    day, month, year = _parse_date(date_part, now) if date_part else (now.day, now.month, now.year)

    try:
        moment = datetime(year, month, day, hour, minute, tzinfo=UTC)
    except ValueError as exc:
        raise ParseError(f"Такой даты не бывает: {text}") from exc

    # Без даты («18:00») имеется в виду ближайшее такое время.
    if not date_part and moment <= now:
        moment += timedelta(days=1)
    return moment


def _parse_time(raw: str) -> tuple[int, int]:
    text = raw.replace(".", ":")
    if ":" not in text:
        if not text.isdigit():
            raise ParseError(f"Не понял время: {raw}")
        return int(text), 0
    hour, _, minute = text.partition(":")
    if not hour.isdigit() or not minute.isdigit():
        raise ParseError(f"Не понял время: {raw}")
    if int(hour) > 23 or int(minute) > 59:
        raise ParseError(f"Такого времени не бывает: {raw}")
    return int(hour), int(minute)


def _parse_date(raw: str, now: datetime) -> tuple[int, int, int]:
    if "-" in raw:  # 2026-09-05
        chunks = raw.split("-")
        if len(chunks) == 3 and all(item.isdigit() for item in chunks):
            return int(chunks[2]), int(chunks[1]), int(chunks[0])
        raise ParseError(f"Не понял дату: {raw}")

    chunks = [item for item in raw.replace("/", ".").split(".") if item]
    if len(chunks) >= 2 and all(item.isdigit() for item in chunks[:2]):
        day, month = int(chunks[0]), int(chunks[1])
        year = int(chunks[2]) if len(chunks) > 2 else now.year
        if year < 100:
            year += 2000
        # «5.01» в декабре — это январь следующего года.
        if len(chunks) == 2 and (month, day) < (now.month, now.day):
            year += 1
        return day, month, year

    raise ParseError(f"Не понял дату: {raw}")


@dataclass(slots=True)
class NewGiveaway:
    title: str
    prize: str = ""
    winners_count: int = 1
    ends_at: datetime | None = None
    channels: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.channels is None:
            self.channels = []


def parse_new(raw: str, now: datetime | None = None) -> NewGiveaway:
    """«Название | приз | 2 | 3д | @channel» → описание розыгрыша.

    Обязательно только название: розыгрыш без срока подводится вручную, один
    победитель — самый частый случай, каналы можно задать общими в .env.
    """
    parts = [part.strip() for part in (raw or "").split("|")]
    if not parts or not parts[0]:
        raise ParseError(
            "Нужно хотя бы название. Пример:\n"
            "/new Кофе в подарок | Пачка зёрен 250 г | 2 | 3д | @mychannel"
        )

    winners = 1
    if len(parts) > 2 and parts[2]:
        digits = "".join(char for char in parts[2] if char.isdigit())
        if not digits:
            raise ParseError(f"Сколько победителей? Не понял «{parts[2]}»")
        winners = max(1, int(digits))

    ends_at = parse_deadline(parts[3], now) if len(parts) > 3 and parts[3] else None
    channels = parse_channels(parts[4]) if len(parts) > 4 else []

    return NewGiveaway(
        title=parts[0],
        prize=parts[1] if len(parts) > 1 else "",
        winners_count=winners,
        ends_at=ends_at,
        channels=channels,
    )
