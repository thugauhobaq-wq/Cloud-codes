"""Модель отзыва — общий формат для всех источников."""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


def _clean(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(text))
    text = html.unescape(text)
    text = text.replace(" ", " ").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_date(value: Any) -> str:
    """Приводит дату любого вида к ISO-строке YYYY-MM-DD."""
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, (int, float)):
        # Секунды или миллисекунды с эпохи.
        ts = float(value)
        if ts > 1e11:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    text = str(value).strip()
    if not text:
        return ""
    if re.fullmatch(r"\d{10,13}", text):
        return parse_date(int(text))

    iso = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso).date().isoformat()
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d", "%d.%m.%y"):
        try:
            return datetime.strptime(text[:10], fmt).date().isoformat()
        except ValueError:
            continue

    # «12 марта 2024» — формат Яндекс.Карт и 2ГИС.
    months = {
        "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
        "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11,
        "декабря": 12,
    }
    m = re.search(r"(\d{1,2})\s+([а-яё]+)\s*(\d{4})?", text.lower())
    if m and m.group(2) in months:
        day = int(m.group(1))
        month = months[m.group(2)]
        year = int(m.group(3)) if m.group(3) else datetime.now().year
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            return ""
    return ""


@dataclass
class Review:
    """Один отзыв, приведённый к общему виду."""

    source: str                      # yandex | 2gis | wildberries | ozon | demo | file
    company: str                     # ключ компании/товара, по которому группируем
    text: str = ""
    rating: float | None = None      # 1..5
    author: str = ""
    date: str = ""                   # ISO YYYY-MM-DD
    url: str = ""
    external_id: str = ""
    reply: str = ""                  # ответ компании, если есть
    pros: str = ""
    cons: str = ""
    sentiment: str = ""              # positive | neutral | negative
    score: float = 0.0               # -1..1
    keywords: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.text = _clean(self.text)
        self.pros = _clean(self.pros)
        self.cons = _clean(self.cons)
        self.reply = _clean(self.reply)
        self.author = _clean(self.author) or "Аноним"
        self.date = parse_date(self.date)
        if self.rating is not None:
            try:
                self.rating = max(1.0, min(5.0, float(self.rating)))
            except (TypeError, ValueError):
                self.rating = None

    @property
    def full_text(self) -> str:
        """Текст отзыва вместе с блоками «достоинства/недостатки» маркетплейсов."""
        parts = []
        if self.pros:
            parts.append(f"Достоинства: {self.pros}")
        if self.cons:
            parts.append(f"Недостатки: {self.cons}")
        if self.text:
            parts.append(self.text)
        return "\n".join(parts)

    @property
    def uid(self) -> str:
        """Стабильный идентификатор для дедупликации между запусками."""
        base = self.external_id or f"{self.author}|{self.date}|{self.full_text[:200]}"
        digest = hashlib.sha1(f"{self.source}|{self.company}|{base}".encode()).hexdigest()
        return digest[:16]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["uid"] = self.uid
        return data
