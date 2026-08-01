"""Расчёт свободных слотов.

Чистая функция от расписания, длительности услуги и занятых промежутков —
ни базы, ни Telegram. Это самая ошибкоопасная часть бота (сетка, границы окна,
переход через полночь, часовой пояс), поэтому её удобно держать отдельно и
целиком закрывать тестами.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from .models import Interval, WorkWindow


def local_midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=tz)


def day_bounds_utc(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Границы локальных суток в UTC — ими выбираем записи за день из базы."""
    start = local_midnight(day, tz)
    # Через +1 день, а не +24 часа: в сутки перехода на летнее время их 23.
    end = local_midnight(day + timedelta(days=1), tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def windows_for(
    windows: Iterable[WorkWindow], day: date, master_id: int | None
) -> list[WorkWindow]:
    """Рабочие окна конкретного мастера на этот день недели.

    Персональное расписание мастера перекрывает общее: если у него заданы свои
    окна, общие для него не действуют — иначе «Аня работает только по субботам»
    было бы невозможно выразить.
    """
    weekday = day.weekday()
    same_day = [item for item in windows if item.weekday == weekday]
    personal = [item for item in same_day if item.master_id == master_id]
    if personal:
        return personal
    return [item for item in same_day if item.master_id is None]


def day_slots(
    *,
    day: date,
    windows: Sequence[WorkWindow],
    duration_min: int,
    busy: Sequence[Interval] = (),
    tz: ZoneInfo,
    now: datetime | None = None,
    step_min: int = 30,
    min_lead_min: int = 0,
    master_id: int | None = None,
) -> list[datetime]:
    """Свободные начала слотов на локальную дату `day`, в UTC и по возрастанию."""
    if duration_min <= 0 or step_min <= 0:
        return []

    now = now or datetime.now(UTC)
    earliest = now + timedelta(minutes=min_lead_min)
    duration = timedelta(minutes=duration_min)
    midnight = local_midnight(day, tz)

    slots: list[datetime] = []
    for window in windows_for(windows, day, master_id):
        offset = window.start_min
        # Услуга должна поместиться в окно целиком: мастер не станет доделывать
        # стрижку после закрытия.
        while offset + duration_min <= window.end_min:
            start_local = midnight + timedelta(minutes=offset)
            offset += step_min

            start = start_local.astimezone(UTC)
            if start < earliest:
                continue

            end = start + duration
            if any(item.overlaps(start, end) for item in busy):
                continue
            slots.append(start)

    return sorted(set(slots))


def has_free_slot(**kwargs) -> bool:
    """Есть ли на дне хоть один слот. Нужен для отметок в календаре."""
    return bool(day_slots(**kwargs))


def horizon_days(today: date, count: int) -> list[date]:
    return [today + timedelta(days=shift) for shift in range(count)]


def merge_intervals(intervals: Sequence[Interval]) -> list[Interval]:
    """Схлопнуть пересекающиеся промежутки в непрерывные.

    Отпуск мастера и его же записи часто накладываются друг на друга; для
    проверки занятости это безразлично, а вот показывать «занято с 10 до 14»
    одним куском приятнее, чем четырьмя.
    """
    if not intervals:
        return []

    ordered = sorted(intervals, key=lambda item: item.start)
    merged = [Interval(ordered[0].start, ordered[0].end)]
    for item in ordered[1:]:
        last = merged[-1]
        if item.start <= last.end:
            last.end = max(last.end, item.end)
        else:
            merged.append(Interval(item.start, item.end))
    return merged


def parse_time(raw: str) -> int:
    """«10:00», «10.00», «10» → минуты от полуночи."""
    text = raw.strip().replace(".", ":").replace("-", ":")
    if ":" not in text:
        if not text.isdigit():
            raise ValueError(f"не похоже на время: {raw!r}")
        hours, minutes = int(text), 0
    else:
        head, _, tail = text.partition(":")
        if not head.isdigit() or not tail.isdigit():
            raise ValueError(f"не похоже на время: {raw!r}")
        hours, minutes = int(head), int(tail)

    if not (0 <= hours <= 24 and 0 <= minutes < 60) or hours * 60 + minutes > 24 * 60:
        raise ValueError(f"такого времени не бывает: {raw!r}")
    return hours * 60 + minutes


def format_minutes(total: int) -> str:
    return f"{total // 60:02d}:{total % 60:02d}"


#: Сокращения дней недели, понятные в командах вида «/hours пн-пт 10:00-20:00».
_WEEKDAY_ALIASES = {
    "пн": 0, "понедельник": 0, "mon": 0,
    "вт": 1, "вторник": 1, "tue": 1,
    "ср": 2, "среда": 2, "wed": 2,
    "чт": 3, "четверг": 3, "thu": 3,
    "пт": 4, "пятница": 4, "fri": 4,
    "сб": 5, "суббота": 5, "sat": 5,
    "вс": 6, "воскресенье": 6, "sun": 6,
}  # fmt: skip

_WEEKDAY_GROUPS = {
    "все": list(range(7)),
    "ежедневно": list(range(7)),
    "будни": [0, 1, 2, 3, 4],
    "выходные": [5, 6],
}


def parse_weekdays(raw: str) -> list[int]:
    """«пн-пт», «сб,вс», «будни», «все» → номера дней недели."""
    text = raw.strip().lower()
    if text in _WEEKDAY_GROUPS:
        return list(_WEEKDAY_GROUPS[text])

    days: set[int] = set()
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            head, _, tail = part.partition("-")
            start, end = _weekday(head), _weekday(tail)
            # Диапазон «сб-вт» разумно понимать через конец недели, а не как ошибку.
            span = (end - start) % 7
            days.update((start + shift) % 7 for shift in range(span + 1))
        else:
            days.add(_weekday(part))
    if not days:
        raise ValueError(f"не понял дни недели: {raw!r}")
    return sorted(days)


def _weekday(token: str) -> int:
    key = token.strip().lower()
    if key not in _WEEKDAY_ALIASES:
        raise ValueError(f"не знаю день недели {token!r}")
    return _WEEKDAY_ALIASES[key]


def parse_windows(raw: str) -> list[tuple[int, int]]:
    """«10:00-20:00» или «10:00-14:00 15:00-20:00» (перерыв) → окна в минутах.

    Слово «выходной» даёт пустой список: так один и тот же синтаксис закрывает
    и рабочие дни, и нерабочие.
    """
    text = raw.strip().lower()
    if text in {"выходной", "выходные", "нет", "-", "закрыто"}:
        return []

    windows: list[tuple[int, int]] = []
    for chunk in text.replace(",", " ").split():
        if "-" not in chunk and "–" not in chunk:
            raise ValueError(f"не понял промежуток {chunk!r}, нужно вида 10:00-20:00")
        head, _, tail = chunk.replace("–", "-").partition("-")
        start, end = parse_time(head), parse_time(tail)
        if start >= end:
            raise ValueError(f"конец раньше начала: {chunk!r}")
        windows.append((start, end))

    if not windows:
        raise ValueError(f"не нашёл ни одного промежутка в {raw!r}")
    return sorted(windows)


def parse_date(raw: str, today: date) -> date:
    """«5.08», «05.08.2026», «2026-08-05», «завтра» → дата.

    Год необязателен: если получившийся день уже прошёл, берём следующий —
    в декабре «5.01» почти наверняка про январь.
    """
    text = raw.strip().lower()
    if not text:
        raise ValueError("Нужна дата, например 5.08")
    if text in {"сегодня", "today"}:
        return today
    if text in {"завтра", "tomorrow"}:
        return today + timedelta(days=1)

    try:
        return date.fromisoformat(text)
    except ValueError:
        pass

    parts = [part for part in text.replace("/", ".").split(".") if part]
    if len(parts) >= 2 and all(part.isdigit() for part in parts[:2]):
        day, month = int(parts[0]), int(parts[1])
        year = int(parts[2]) if len(parts) > 2 else today.year
        if year < 100:
            year += 2000
        try:
            result = date(year, month, day)
        except ValueError as exc:
            raise ValueError(f"Такой даты нет: {raw}") from exc
        if len(parts) == 2 and result < today:
            result = result.replace(year=year + 1)
        return result

    raise ValueError(f"Не разобрал дату {raw!r}. Примеры: 5.08, 05.08.2026, завтра")


def parse_schedule_spec(raw: str) -> tuple[list[int], list[tuple[int, int]]]:
    """«пн-пт 10:00-20:00» → ([0,1,2,3,4], [(600, 1200)])."""
    parts = raw.strip().split(maxsplit=1)
    if len(parts) < 2:
        raise ValueError("нужно указать дни и время, например: пн-пт 10:00-20:00")
    return parse_weekdays(parts[0]), parse_windows(parts[1])
