"""Антиспам и автомодерация.

Форма отзыва открыта наружу, поэтому в неё будут лить рекламу. Здесь
дешёвые проверки, которые ловят основную массу мусора до попадания в базу,
и решение о том, публиковать ли отзыв сразу.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from .models import Review, Site

#: Ссылка целиком, а не по кускам: иначе «www.example.ru» посчиталось бы дважды.
_URL_RE = re.compile(
    r"(?:https?://|www\.)\S+"
    r"|\b[a-zа-я0-9-]+\.(?:ru|рф|com|net|org|info|biz|xyz|top|site|online|shop)\b",
    re.IGNORECASE,
)
_REPEAT_RE = re.compile(r"(.)\1{6,}")
_PHONE_RE = re.compile(r"(?:\+?\d[\s\-()]?){10,}")

#: Основы слов, по которым узнаётся типовая реклама в отзывах. Это основы, а
#: не подстроки: искать «ставк» где угодно нельзя, иначе в каждой «доставке»
#: найдётся спам. Поэтому сравнение идёт от начала слова.
SPAM_WORDS = (
    "заработок", "казино", "ставк", "крипто", "биткоин", "инвестиц", "форекс",
    "порно", "виагра", "займ", "кредит без", "seo продвижение", "накрутк",
    "телеграм канал", "промокод", "http://t.me", "whatsapp +",
)

_SPAM_RE = re.compile(
    "|".join(r"\b" + re.escape(word) for word in SPAM_WORDS), re.IGNORECASE
)

MIN_FILL_SECONDS = 3.0   # быстрее человек форму не заполнит
MAX_PER_MINUTE = 5       # на один IP
MAX_PER_HOUR = 20        # на один сайт с одного IP


@dataclass
class Verdict:
    ok: bool
    reason: str = ""
    score: float = 0.0          # 0 — чисто, 1 — очевидный спам
    flags: list[str] = None     # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.flags is None:
            self.flags = []


def check_text(text: str, author: str = "") -> Verdict:
    """Оценивает текст отзыва. Не блокирует по одному признаку — считает баллы."""
    flags: list[str] = []
    score = 0.0
    lowered = f"{text}\n{author}".lower()

    links = len(_URL_RE.findall(text))
    if links:
        flags.append("links")
        score += 0.45 + 0.15 * min(links - 1, 3)
    if _PHONE_RE.search(text):
        flags.append("phone")
        score += 0.25
    hits = set(match.group(0).lower() for match in _SPAM_RE.finditer(lowered))
    if hits:
        flags.append("spam-words")
        score += 0.4 + 0.2 * min(len(hits) - 1, 2)
    if _REPEAT_RE.search(text):
        flags.append("repeats")
        score += 0.3

    letters = [ch for ch in text if ch.isalpha()]
    if letters:
        caps = sum(1 for ch in letters if ch.isupper()) / len(letters)
        if caps > 0.6 and len(letters) > 20:
            flags.append("caps")
            score += 0.25

    words = [w for w in re.split(r"\W+", text.lower()) if len(w) > 2]
    if len(words) > 8 and len(set(words)) / len(words) < 0.4:
        flags.append("repetitive")
        score += 0.3

    score = min(score, 1.0)
    if score >= 0.6:
        return Verdict(False, "Похоже на рекламу — отзыв не принят", score, flags)
    return Verdict(True, "", score, flags)


class RateLimiter:
    """Скользящее окно по IP. В памяти процесса — базу этим не мучаем."""

    def __init__(self, per_minute: int = MAX_PER_MINUTE, per_hour: int = MAX_PER_HOUR) -> None:
        self.per_minute = per_minute
        self.per_hour = per_hour
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        hits = self._hits[key]
        while hits and now - hits[0] > 3600:
            hits.popleft()
        if len(hits) >= self.per_hour:
            return False
        if sum(1 for t in hits if now - t <= 60) >= self.per_minute:
            return False
        hits.append(now)
        return True

    def forget(self, key: str) -> None:
        self._hits.pop(key, None)


def check_submission(payload: dict[str, Any]) -> Verdict:
    """Проверки самой отправки: honeypot и время заполнения формы."""
    # Скрытое поле, которое видят только боты.
    if str(payload.get("website") or payload.get("hp") or "").strip():
        return Verdict(False, "Отзыв не принят", 1.0, ["honeypot"])

    try:
        elapsed = float(payload.get("elapsed", 0) or 0)
    except (TypeError, ValueError):
        elapsed = 0.0
    if 0 < elapsed < MIN_FILL_SECONDS:
        return Verdict(False, "Слишком быстро — попробуйте ещё раз", 0.8, ["too-fast"])
    return Verdict(True)


def decide_status(site: Site, review: Review, verdict: Verdict) -> str:
    """Публиковать сразу или отправить на модерацию."""
    if not site.auto_approve:
        return "pending"
    if review.rating < site.auto_approve_from:
        return "pending"
    if verdict.score >= 0.2 or verdict.flags:
        # Подозрительное всегда смотрит человек, даже при автопубликации.
        return "pending"
    return "published"
