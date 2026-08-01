"""Хранилище отзывов на SQLite (без внешних зависимостей)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .models import Review

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "reviews.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    uid         TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    company     TEXT NOT NULL,
    text        TEXT,
    pros        TEXT,
    cons        TEXT,
    reply       TEXT,
    rating      REAL,
    author      TEXT,
    date        TEXT,
    url         TEXT,
    external_id TEXT,
    sentiment   TEXT,
    score       REAL,
    keywords    TEXT,
    collected_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_company ON reviews(company);
CREATE INDEX IF NOT EXISTS idx_source  ON reviews(source);
CREATE INDEX IF NOT EXISTS idx_date    ON reviews(date);
"""


class Storage:
    """Тонкая обёртка над SQLite: сохранение с дедупликацией и выборки."""

    def __init__(self, path: str | Path = DEFAULT_DB) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ запись

    def save(self, reviews: Iterable[Review]) -> int:
        """Сохраняет отзывы, пропуская уже известные. Возвращает число новых."""
        rows = [
            (
                review.uid, review.source, review.company, review.text,
                review.pros, review.cons, review.reply, review.rating,
                review.author, review.date, review.url, review.external_id,
                review.sentiment, review.score, json.dumps(review.keywords, ensure_ascii=False),
            )
            for review in reviews
        ]
        if not rows:
            return 0
        before = self.count()
        self._conn.executemany(
            """INSERT OR IGNORE INTO reviews
               (uid, source, company, text, pros, cons, reply, rating, author,
                date, url, external_id, sentiment, score, keywords)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        self._conn.commit()
        return self.count() - before

    def delete_company(self, company: str) -> int:
        cursor = self._conn.execute("DELETE FROM reviews WHERE company = ?", (company,))
        self._conn.commit()
        return cursor.rowcount

    # ------------------------------------------------------------------ чтение

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]

    def companies(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT company, COUNT(*) AS total, AVG(rating) AS avg_rating,
                      MAX(date) AS last_date
               FROM reviews GROUP BY company ORDER BY total DESC"""
        ).fetchall()
        return [
            {
                "company": row["company"],
                "total": row["total"],
                "avg_rating": round(row["avg_rating"], 2) if row["avg_rating"] else None,
                "last_date": row["last_date"] or "",
            }
            for row in rows
        ]

    def fetch(
        self,
        company: str | None = None,
        source: str | None = None,
        sentiment: str | None = None,
        query: str | None = None,
        date_from: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM reviews WHERE 1=1"
        params: list[Any] = []
        if company:
            sql += " AND company = ?"
            params.append(company)
        if source:
            sql += " AND source = ?"
            params.append(source)
        if sentiment:
            sql += " AND sentiment = ?"
            params.append(sentiment)
        if date_from:
            sql += " AND date >= ?"
            params.append(date_from)
        if query:
            sql += " AND (text LIKE ? OR pros LIKE ? OR cons LIKE ? OR author LIKE ?)"
            like = f"%{query}%"
            params.extend([like, like, like, like])
        sql += " ORDER BY date DESC, uid"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def total_matching(self, **filters: Any) -> int:
        filters.pop("limit", None)
        filters.pop("offset", None)
        return len(self.fetch(**filters))

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        try:
            data["keywords"] = json.loads(data.get("keywords") or "[]")
        except (json.JSONDecodeError, TypeError):
            data["keywords"] = []
        return data
