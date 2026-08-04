"""Универсальный импорт отзывов из локального JSON или CSV.

Полезно, когда выгрузка уже есть: из личного кабинета маркетплейса,
из чужого парсера или сохранённая вручную.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from ..httpclient import FetchError
from ..models import Review
from .base import Source

FIELD_ALIASES = {
    "text": ("text", "comment", "review", "body", "отзыв", "текст", "содержание"),
    "rating": ("rating", "score", "stars", "productValuation", "оценка", "рейтинг"),
    "author": ("author", "user", "name", "автор", "имя"),
    "date": ("date", "created", "createdDate", "date_created", "дата"),
    "url": ("url", "link", "ссылка"),
    "pros": ("pros", "advantages", "достоинства", "плюсы"),
    "cons": ("cons", "disadvantages", "недостатки", "минусы"),
    "reply": ("reply", "answer", "ответ"),
    "source": ("source", "platform", "источник", "площадка"),
    "external_id": ("id", "uid", "external_id"),
}


def _pick(row: dict[str, Any], field: str) -> Any:
    lowered = {str(key).strip().lower(): value for key, value in row.items()}
    for alias in FIELD_ALIASES[field]:
        if alias.lower() in lowered and lowered[alias.lower()] not in (None, ""):
            return lowered[alias.lower()]
    return ""


def rows_to_reviews(rows: list[dict[str, Any]], company: str, default_source: str = "file") -> list[Review]:
    reviews = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        rating = _pick(row, "rating")
        try:
            rating_value = float(str(rating).replace(",", ".")) if rating != "" else None
        except ValueError:
            rating_value = None
        reviews.append(
            Review(
                source=str(_pick(row, "source") or default_source),
                company=company,
                text=str(_pick(row, "text")),
                pros=str(_pick(row, "pros")),
                cons=str(_pick(row, "cons")),
                reply=str(_pick(row, "reply")),
                rating=rating_value,
                author=str(_pick(row, "author")),
                date=_pick(row, "date"),
                url=str(_pick(row, "url")),
                external_id=str(_pick(row, "external_id") or index),
            )
        )
    return reviews


def parse_text(payload: str, company: str, hint: str = "") -> list[Review]:
    """Разбирает JSON (список или {"reviews": [...]}) либо CSV."""
    stripped = payload.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        data = json.loads(payload)
        if isinstance(data, dict):
            for key in ("reviews", "items", "data", "feedbacks"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
            else:
                data = [data]
        return rows_to_reviews(data, company)

    dialect_sample = payload[:2048]
    try:
        dialect = csv.Sniffer().sniff(dialect_sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(payload), dialect=dialect)
    return rows_to_reviews(list(reader), company)


class FileSource(Source):
    name = "file"
    title = "Файл (JSON/CSV)"
    query_hint = "путь к файлу выгрузки, например data/export.csv"
    needs_key = False

    def collect(self, query: str, limit: int = 1000, **options: Any) -> list[Review]:
        path = Path(query).expanduser()
        if not path.exists():
            raise FetchError(f"Файл не найден: {path}")
        company = self.company_key(options.get("company") or path.stem)
        return parse_text(path.read_text(encoding="utf-8", errors="replace"), company)[:limit]

    def parse_payload(self, payload: str, company: str) -> list[Review]:
        return parse_text(payload, company)
