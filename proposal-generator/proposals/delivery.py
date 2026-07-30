"""Судьба предложения после того, как его собрали: отправка, просмотр, решение.

Модель документа (`models.py`) про то, что напечатано на бумаге. Здесь — про то,
что с этой бумагой произошло: кому ушла, когда открыли, чем кончилось.

Как узнаётся «просмотрено». В письмо кладётся и PDF, и ссылка на страницу
предложения; отметка ставится, когда открыли именно ссылку. Пиксель-трекер в
письме мы намеренно не используем: почтовые клиенты режут картинки, так что
цифра всё равно врёт, а следить за человеком без его ведома — плохой обмен.
Значит, открытое из письма вложение мимо ссылки останется незамеченным, и это
честнее, чем ложное «просмотрено».
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime

DRAFT = "draft"
"""Ещё не отправляли."""

SENT = "sent"
"""Письмо ушло."""

VIEWED = "viewed"
"""Заказчик открыл ссылку."""

ACCEPTED = "accepted"
DECLINED = "declined"

STATUSES = (DRAFT, SENT, VIEWED, ACCEPTED, DECLINED)

# Ранг нужен, чтобы поздний просмотр не сбросил уже принятое предложение:
# заказчик вполне может открыть ссылку ещё раз после того, как согласился.
_RANK = {DRAFT: 0, SENT: 1, VIEWED: 2, ACCEPTED: 3, DECLINED: 3}

STATUS_LABELS = {
    DRAFT: "черновик",
    SENT: "отправлено",
    VIEWED: "просмотрено",
    ACCEPTED: "принято",
    DECLINED: "отклонено",
}

EVENT_LABELS = {
    "sent": "отправлено",
    "resent": "отправлено повторно",
    "viewed": "открыто по ссылке",
    "accepted": "заказчик принял",
    "declined": "заказчик хочет обсудить",
    "reminded": "отправлено напоминание",
    "reset": "возвращено в черновики",
}


def _int(value: object) -> int:
    try:
        return max(int(str(value or 0)), 0)
    except (TypeError, ValueError):
        return 0


def now() -> datetime:
    """Текущий момент в UTC.

    Со временем работаем только в UTC: черновик легко переезжает между
    машинами, а «вчера в 18:00» без пояса потом не восстановить.
    """
    return datetime.now(UTC).replace(microsecond=0)


def parse_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def new_token() -> str:
    """Секрет публичной ссылки.

    32 байта — ссылку нельзя подобрать перебором, а знание токена и есть
    единственное право видеть предложение.
    """
    return secrets.token_urlsafe(32)


@dataclass(slots=True)
class Event:
    """Строка журнала: что случилось и когда."""

    kind: str
    at: datetime = field(default_factory=now)
    detail: str = ""

    @classmethod
    def from_dict(cls, raw: dict) -> Event:
        return cls(
            kind=str(raw.get("kind") or "").strip() or "unknown",
            at=parse_time(raw.get("at")) or now(),
            detail=str(raw.get("detail") or ""),
        )

    def to_dict(self) -> dict:
        return {"kind": self.kind, "at": self.at.isoformat(), "detail": self.detail}

    @property
    def label(self) -> str:
        return EVENT_LABELS.get(self.kind, self.kind)


@dataclass(slots=True)
class Delivery:
    """Состояние доставки одного предложения."""

    status: str = DRAFT
    token: str = ""
    recipient: str = ""
    sent_at: datetime | None = None
    viewed_at: datetime | None = None
    decided_at: datetime | None = None
    comment: str = ""
    """Что заказчик написал, нажимая «принять» или «обсудить»."""
    reminded_at: datetime | None = None
    reminders: int = 0
    events: list[Event] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict | None) -> Delivery:
        raw = raw or {}
        status = str(raw.get("status") or DRAFT)
        return cls(
            status=status if status in STATUSES else DRAFT,
            token=str(raw.get("token") or ""),
            recipient=str(raw.get("recipient") or ""),
            sent_at=parse_time(raw.get("sent_at")),
            viewed_at=parse_time(raw.get("viewed_at")),
            decided_at=parse_time(raw.get("decided_at")),
            comment=str(raw.get("comment") or ""),
            reminded_at=parse_time(raw.get("reminded_at")),
            reminders=_int(raw.get("reminders")),
            events=[
                Event.from_dict(item)
                for item in raw.get("events") or []
                if isinstance(item, dict)
            ],
        )

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "token": self.token,
            "recipient": self.recipient,
            "sent_at": self.sent_at.isoformat() if self.sent_at else "",
            "viewed_at": self.viewed_at.isoformat() if self.viewed_at else "",
            "decided_at": self.decided_at.isoformat() if self.decided_at else "",
            "comment": self.comment,
            "reminded_at": self.reminded_at.isoformat() if self.reminded_at else "",
            "reminders": self.reminders,
            "events": [event.to_dict() for event in self.events],
        }

    def public_dict(self) -> dict:
        """То, что можно отдать наружу: без токена и без адреса получателя."""
        return {
            "status": self.status,
            "status_label": self.label,
            "decided_at": self.decided_at.isoformat() if self.decided_at else "",
            "comment": self.comment,
        }

    # --- переходы ------------------------------------------------------------

    @property
    def label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def is_decided(self) -> bool:
        return self.status in (ACCEPTED, DECLINED)

    def _advance(self, status: str) -> None:
        """Поднять статус, но никогда не понижать."""
        if _RANK.get(status, 0) >= _RANK.get(self.status, 0):
            self.status = status

    def ensure_token(self) -> str:
        if not self.token:
            self.token = new_token()
        return self.token

    def matches(self, token: str) -> bool:
        """Сравнение токенов за постоянное время.

        Сравниваем байты, а не строки: токен приходит из адресной строки и
        вполне может оказаться кириллицей, а `compare_digest` на не-ASCII
        строках бросает TypeError — вместо честного «ссылка не найдена»
        получилась бы ошибка сервера.
        """
        if not self.token or not token:
            return False
        return secrets.compare_digest(self.token.encode("utf-8"), str(token).encode("utf-8"))

    def mark_sent(self, recipient: str) -> None:
        first = self.sent_at is None
        self.ensure_token()
        self.recipient = recipient
        self.sent_at = self.sent_at or now()
        self._advance(SENT)
        self.events.append(Event("sent" if first else "resent", detail=recipient))

    def mark_viewed(self) -> bool:
        """Отметить открытие ссылки. Возвращает True, если это первый раз.

        Повторные заходы в журнал не пишем: заказчик может открыть письмо
        десять раз, и журнал из десяти одинаковых строк никому не помогает.
        """
        first = self.viewed_at is None
        if first:
            self.viewed_at = now()
            self.events.append(Event("viewed"))
        self._advance(VIEWED)
        return first

    def decide(self, accepted: bool, comment: str = "") -> None:
        self.decided_at = now()
        self.comment = comment.strip()
        status = ACCEPTED if accepted else DECLINED
        self.status = status  # решение можно поменять — оно последнее слово
        self.events.append(Event(status, detail=self.comment))

    def reset(self) -> None:
        """Вернуть в черновики: новый токен, старая ссылка перестаёт работать."""
        self.status = DRAFT
        self.token = ""
        self.recipient = ""
        self.sent_at = self.viewed_at = self.decided_at = self.reminded_at = None
        self.comment = ""
        self.reminders = 0
        self.events.append(Event("reset"))

    def mark_reminded(self) -> None:
        self.reminded_at = now()
        self.reminders += 1
        self.events.append(Event("reminded", detail=self.recipient))

    @property
    def waiting_since(self) -> datetime | None:
        """С какого момента идёт отсчёт молчания.

        Если напоминание уже отправляли, считаем от него, а не от первого
        письма, — иначе одно и то же предложение будет «просрочено» вечно и
        напоминания посыплются каждый день.
        """
        if self.status not in (SENT, VIEWED):
            return None
        return self.reminded_at or self.sent_at

    def silent_days(self, at: datetime | None = None) -> int | None:
        """Сколько полных суток заказчик молчит. `None` — отсчёт не идёт."""
        since = self.waiting_since
        if since is None:
            return None
        return max(((at or now()) - since).days, 0)

    def needs_reminder(self, after_days: int, at: datetime | None = None) -> bool:
        days = self.silent_days(at)
        return days is not None and days >= max(after_days, 0)

    def link(self, base_url: str) -> str:
        """Публичная ссылка на предложение."""
        if not self.token:
            return ""
        return f"{base_url.rstrip('/')}/p/{self.token}"


# Отправленным считается всё, что когда-либо ушло заказчику, — иначе принятое
# предложение выпадало бы из знаменателя конверсии.
DELIVERED = (SENT, VIEWED, ACCEPTED, DECLINED)


def summarize(items: list[tuple[str, object, str]]) -> dict:
    """Воронка по списку `(статус, сумма, валюта)`.

    Суммы складываем внутри валюты: смешивать рубли с евро нельзя, а
    пересчитывать по курсу — не дело генератора предложений.
    """
    from decimal import Decimal

    counts = dict.fromkeys(STATUSES, 0)
    money: dict[str, dict[str, Decimal]] = {}

    for status, total, currency in items:
        status = status if status in counts else DRAFT
        counts[status] += 1
        amount = total if isinstance(total, Decimal) else Decimal(str(total or 0))
        bucket = money.setdefault(
            currency or "RUB",
            {"delivered": Decimal("0"), "accepted": Decimal("0"), "open": Decimal("0")},
        )
        if status in DELIVERED:
            bucket["delivered"] += amount
        if status == ACCEPTED:
            bucket["accepted"] += amount
        if status in (SENT, VIEWED):
            bucket["open"] += amount  # ушло, но ответа ещё нет

    delivered = sum(counts[status] for status in DELIVERED)
    return {
        "counts": counts,
        "total": sum(counts.values()),
        "delivered": delivered,
        "conversion": round(counts[ACCEPTED] / delivered * 100, 1) if delivered else 0.0,
        "money": {
            currency: {key: str(value) for key, value in bucket.items()}
            for currency, bucket in money.items()
        },
    }
