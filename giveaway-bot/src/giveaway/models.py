"""Сущности: розыгрыш, участник, победитель."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

STATUS_ACTIVE = "active"
STATUS_FINISHED = "finished"
STATUS_CANCELLED = "cancelled"

STATUS_TITLES = {
    STATUS_ACTIVE: "🟢 идёт",
    STATUS_FINISHED: "🏁 завершён",
    STATUS_CANCELLED: "❌ отменён",
}


@dataclass(slots=True)
class Giveaway:
    id: int
    title: str
    prize: str = ""
    winners_count: int = 1
    status: str = STATUS_ACTIVE
    ends_at: datetime | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None
    #: Каналы, подписку на которые проверяем перед участием.
    channels: list[str] = field(default_factory=list)
    #: Зерно жребия. Появляется при завершении и публикуется вместе с итогами.
    seed: str = ""
    participants: int = 0

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE

    def time_left(self, now: datetime | None = None) -> timedelta:
        if self.ends_at is None:
            return timedelta(0)
        return self.ends_at - (now or datetime.now(UTC))

    def is_due(self, now: datetime | None = None) -> bool:
        """Пора подводить итоги."""
        if not self.is_active or self.ends_at is None:
            return False
        return self.time_left(now).total_seconds() <= 0


@dataclass(slots=True)
class Participant:
    giveaway_id: int
    tg_id: int
    name: str = ""
    username: str | None = None
    number: int = 0
    joined_at: datetime | None = None


@dataclass(slots=True)
class Winner:
    giveaway_id: int
    tg_id: int
    place: int
    seed: str = ""
    round_number: int = 0
    notified: bool = False
    name: str = ""
    username: str | None = None
