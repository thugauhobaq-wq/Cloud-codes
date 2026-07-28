"""Агрегации для дашборда: сводка, динамика, аспекты, ключевые слова."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Any, Iterable

from . import sentiment as st

_WORD_RE = re.compile(r"[а-яёa-z]{4,}", re.IGNORECASE)


def _full_text(row: dict[str, Any]) -> str:
    parts = [row.get("pros") or "", row.get("cons") or "", row.get("text") or ""]
    return "\n".join(part for part in parts if part)


def summarize(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Считает всю сводку по списку отзывов одним проходом."""
    rows = list(rows)
    total = len(rows)
    empty = {
        "total": 0, "avg_rating": None, "avg_score": 0.0, "with_reply": 0,
        "reply_rate": 0.0, "sentiment": {"positive": 0, "neutral": 0, "negative": 0},
        "sentiment_share": {"positive": 0.0, "neutral": 0.0, "negative": 0.0},
        "ratings": {str(i): 0 for i in range(1, 6)}, "sources": [], "timeline": [],
        "aspects": [], "keywords": {"positive": [], "negative": []},
        "highlights": {}, "trend": {"delta": 0.0, "recent_negative_share": 0.0,
                                    "previous_negative_share": 0.0, "status": "нет данных"},
        "period": {"from": "", "to": ""},
    }
    if not total:
        return empty

    sentiment_counts = Counter()
    ratings = Counter()
    per_source: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"total": 0, "rating_sum": 0.0, "rating_n": 0, "score_sum": 0.0,
                 "positive": 0, "neutral": 0, "negative": 0}
    )
    monthly: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"total": 0, "score_sum": 0.0, "rating_sum": 0.0, "rating_n": 0,
                 "positive": 0, "neutral": 0, "negative": 0}
    )
    aspect_totals: dict[str, list[float]] = defaultdict(list)
    positive_words: Counter = Counter()
    negative_words: Counter = Counter()

    rating_sum = 0.0
    rating_n = 0
    score_sum = 0.0
    with_reply = 0
    dates: list[str] = []

    for row in rows:
        label = row.get("sentiment") or "neutral"
        score = float(row.get("score") or 0.0)
        source = row.get("source") or "unknown"
        rating = row.get("rating")
        text = _full_text(row)

        sentiment_counts[label] += 1
        score_sum += score
        if row.get("reply"):
            with_reply += 1
        if row.get("date"):
            dates.append(row["date"])

        if rating is not None:
            rating_sum += float(rating)
            rating_n += 1
            ratings[str(int(round(float(rating))))] += 1

        bucket = per_source[source]
        bucket["total"] += 1
        bucket["score_sum"] += score
        bucket[label] = bucket.get(label, 0) + 1
        if rating is not None:
            bucket["rating_sum"] += float(rating)
            bucket["rating_n"] += 1

        month = (row.get("date") or "")[:7]
        if month:
            point = monthly[month]
            point["total"] += 1
            point["score_sum"] += score
            point[label] = point.get(label, 0) + 1
            if rating is not None:
                point["rating_sum"] += float(rating)
                point["rating_n"] += 1

        result = st.analyze(text, rating)
        for aspect, value in result.aspects.items():
            aspect_totals[aspect].append(value)

        target = positive_words if label == "positive" else negative_words if label == "negative" else None
        if target is not None:
            for word in _WORD_RE.findall(text.lower()):
                if word not in st.STOPWORDS:
                    target[word] += 1

    sources = sorted(
        (
            {
                "source": name,
                "total": data["total"],
                "avg_rating": round(data["rating_sum"] / data["rating_n"], 2) if data["rating_n"] else None,
                "avg_score": round(data["score_sum"] / data["total"], 3),
                "positive": data.get("positive", 0),
                "neutral": data.get("neutral", 0),
                "negative": data.get("negative", 0),
            }
            for name, data in per_source.items()
        ),
        key=lambda item: -item["total"],
    )

    timeline = [
        {
            "month": month,
            "total": data["total"],
            "avg_score": round(data["score_sum"] / data["total"], 3),
            "avg_rating": round(data["rating_sum"] / data["rating_n"], 2) if data["rating_n"] else None,
            "positive": data.get("positive", 0),
            "neutral": data.get("neutral", 0),
            "negative": data.get("negative", 0),
        }
        for month, data in sorted(monthly.items())
    ]

    aspects = sorted(
        (
            {
                "aspect": name,
                "score": round(sum(values) / len(values), 3),
                "mentions": len(values),
            }
            for name, values in aspect_totals.items()
        ),
        key=lambda item: -item["mentions"],
    )

    return {
        "total": total,
        "avg_rating": round(rating_sum / rating_n, 2) if rating_n else None,
        "avg_score": round(score_sum / total, 3),
        "with_reply": with_reply,
        "reply_rate": round(with_reply / total, 3),
        "sentiment": {
            "positive": sentiment_counts["positive"],
            "neutral": sentiment_counts["neutral"],
            "negative": sentiment_counts["negative"],
        },
        "sentiment_share": {
            key: round(sentiment_counts[key] / total, 3)
            for key in ("positive", "neutral", "negative")
        },
        "ratings": {str(i): ratings[str(i)] for i in range(1, 6)},
        "sources": sources,
        "timeline": timeline,
        "aspects": aspects,
        "keywords": {
            "positive": [{"word": w, "count": c} for w, c in positive_words.most_common(20)],
            "negative": [{"word": w, "count": c} for w, c in negative_words.most_common(20)],
        },
        "highlights": _highlights(rows),
        "trend": _trend(rows),
        "period": {"from": min(dates) if dates else "", "to": max(dates) if dates else ""},
    }


def _short(row: dict[str, Any], limit: int = 220) -> dict[str, Any]:
    text = _full_text(row)
    return {
        "author": row.get("author") or "Аноним",
        "date": row.get("date") or "",
        "source": row.get("source") or "",
        "rating": row.get("rating"),
        "score": row.get("score"),
        "sentiment": row.get("sentiment"),
        "url": row.get("url") or "",
        "text": text if len(text) <= limit else text[:limit].rstrip() + "…",
    }


def _highlights(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [row for row in rows if _full_text(row)]
    if not scored:
        return {}
    best = max(scored, key=lambda row: float(row.get("score") or 0))
    worst = min(scored, key=lambda row: float(row.get("score") or 0))
    latest = max(scored, key=lambda row: row.get("date") or "")
    return {"best": _short(best), "worst": _short(worst), "latest": _short(latest)}


def _trend(rows: list[dict[str, Any]], window_days: int = 30) -> dict[str, Any]:
    """Сравнивает долю негатива за последние 30 дней и предыдущие 30."""
    dated = [row for row in rows if row.get("date")]
    if not dated:
        return {"delta": 0.0, "recent_negative_share": 0.0,
                "previous_negative_share": 0.0, "status": "нет данных"}

    last_date = max(row["date"] for row in dated)
    try:
        anchor = date.fromisoformat(last_date)
    except ValueError:
        return {"delta": 0.0, "recent_negative_share": 0.0,
                "previous_negative_share": 0.0, "status": "нет данных"}

    recent_start = (anchor - timedelta(days=window_days)).isoformat()
    prev_start = (anchor - timedelta(days=window_days * 2)).isoformat()

    recent = [row for row in dated if row["date"] >= recent_start]
    previous = [row for row in dated if prev_start <= row["date"] < recent_start]

    def negative_share(items: list[dict[str, Any]]) -> float:
        if not items:
            return 0.0
        return sum(1 for row in items if row.get("sentiment") == "negative") / len(items)

    recent_share = negative_share(recent)
    previous_share = negative_share(previous)
    delta = recent_share - previous_share

    if not previous:
        status = "недостаточно истории"
    elif delta > 0.07:
        status = "негатив растёт"
    elif delta < -0.07:
        status = "негатив снижается"
    else:
        status = "стабильно"

    return {
        "delta": round(delta, 3),
        "recent_negative_share": round(recent_share, 3),
        "previous_negative_share": round(previous_share, 3),
        "recent_total": len(recent),
        "previous_total": len(previous),
        "status": status,
    }
