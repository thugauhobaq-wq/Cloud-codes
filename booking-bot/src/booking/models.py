"""Сущности предметной области.

Все моменты времени внутри программы — aware datetime в UTC. В локальное время
переводим только на границе с пользователем (см. `texts.py`): иначе при переходе
на летнее время и обратно записи разъезжаются.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

#: Понедельник = 0, как в `date.weekday()`.
WEEKDAY_NAMES = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

STATUS_ACTIVE = "active"
STATUS_CANCELLED = "cancelled"
STATUS_DONE = "done"


@dataclass(slots=True)
class Service:
    """Услуга: то, на что записываются."""

    id: int
    title: str
    duration_min: int
    price: int | None = None
    description: str = ""
    active: bool = True
    position: int = 0

    @property
    def duration(self) -> timedelta:
        return timedelta(minutes=self.duration_min)


@dataclass(slots=True)
class Master:
    """Мастер (специалист). Если он один, бот не спрашивает про выбор."""

    id: int
    name: str
    active: bool = True


@dataclass(slots=True)
class WorkWindow:
    """Рабочее окно в расписании: день недели и промежуток в минутах от полуночи.

    Минуты, а не `time`, потому что окно вида 10:00–20:00 удобно и хранить, и
    сравнивать целыми числами, а секунды здесь никому не нужны.
    """

    weekday: int
    start_min: int
    end_min: int
    master_id: int | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.weekday <= 6:
            raise ValueError(f"день недели вне диапазона: {self.weekday}")
        if not 0 <= self.start_min < self.end_min <= 24 * 60:
            raise ValueError(f"некорректное окно: {self.start_min}–{self.end_min}")


@dataclass(slots=True)
class Interval:
    """Занятый промежуток: чужая запись, перерыв или отпуск."""

    start: datetime
    end: datetime

    def overlaps(self, other_start: datetime, other_end: datetime) -> bool:
        # Границы не считаем пересечением: запись 12:00–13:00 не мешает 13:00–14:00.
        return self.start < other_end and other_start < self.end


@dataclass(slots=True)
class Client:
    tg_id: int
    name: str
    phone: str = ""
    username: str | None = None


@dataclass(slots=True)
class Booking:
    """Запись клиента. `starts_at` и `ends_at` — UTC."""

    id: int
    client_id: int
    service_id: int
    starts_at: datetime
    ends_at: datetime
    master_id: int | None = None
    status: str = STATUS_ACTIVE
    comment: str = ""
    created_at: datetime | None = None
    reminded_at: datetime | None = None

    # Подтягиваются джойном — нужны почти в каждом сообщении, а отдельный
    # запрос за названием услуги на каждую строку списка не имеет смысла.
    service_title: str = ""
    master_name: str = ""
    client_name: str = ""
    client_phone: str = ""

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE

    def starts_in(self, now: datetime | None = None) -> timedelta:
        return self.starts_at - (now or datetime.now(UTC))
