"""Конвейер: собрать → разметить тональность → сохранить."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from . import sentiment as st
from .models import Review
from .sources import get_source
from .storage import Storage


def enrich(reviews: Iterable[Review]) -> list[Review]:
    """Проставляет тональность, score и ключевые слова каждому отзыву."""
    result = []
    for review in reviews:
        analysis = st.analyze(review.full_text, review.rating)
        review.sentiment = analysis.label
        review.score = analysis.score
        review.keywords = analysis.keywords
        result.append(review)
    return result


def collect(
    source_name: str,
    query: str,
    limit: int = 100,
    company: str | None = None,
    storage: Storage | None = None,
    from_file: str | None = None,
    **options: Any,
) -> dict[str, Any]:
    """Собирает отзывы одним источником и складывает их в базу.

    ``from_file`` разбирает заранее сохранённый ответ площадки — режим для
    сайтов, которые не пускают роботов.
    """
    source = get_source(source_name)
    company_key = company or source.company_key(query) or source_name

    if from_file:
        payload = Path(from_file).expanduser().read_text(encoding="utf-8", errors="replace")
        reviews = source.parse_payload(payload, company_key)
    else:
        reviews = source.collect(query, limit=limit, company=company_key, **options)

    reviews = enrich(reviews)[:limit] if limit else enrich(reviews)

    own_storage = storage is None
    storage = storage or Storage()
    try:
        added = storage.save(reviews)
    finally:
        if own_storage:
            storage.close()

    return {
        "source": source_name,
        "company": company_key,
        "fetched": len(reviews),
        "added": added,
        "duplicates": len(reviews) - added,
    }
