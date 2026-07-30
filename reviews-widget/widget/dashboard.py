"""Импорт отзывов из соседнего проекта `reviews-dashboard`.

Дашборд уже умеет собирать отзывы с Яндекс.Карт, 2ГИС, Wildberries и Ozon.
Здесь мы забираем готовые отзывы из его базы и показываем их на сайте
клиента — то есть виджет показывает не только то, что клиент собрал сам, но
и то, что о нём уже написали на площадках.

Связь односторонняя и читающая: базу дашборда открываем в режиме `mode=ro`,
а его Python-код не импортируем. Виджет должен ставиться на сервер, где
никакого дашборда рядом может и не быть.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .models import Review, Site, ValidationError, clean_text
from .moderation import check_text
from .storage import Storage

#: Как называются площадки в базе дашборда и как их подписывать в виджете.
PLATFORMS: dict[str, str] = {
    "yandex": "Яндекс.Карты",
    "2gis": "2ГИС",
    "wildberries": "Wildberries",
    "ozon": "Ozon",
    "file": "выгрузка",
    "demo": "демо",
}

REQUIRED_COLUMNS = {"source", "company", "text", "rating", "author", "date"}

#: Где лежит база дашборда, если оба проекта стоят рядом в этом репозитории.
DEFAULT_DASHBOARD_DB = (
    Path(__file__).resolve().parent.parent.parent / "reviews-dashboard" / "data" / "reviews.db"
)


class DashboardError(RuntimeError):
    """База дашборда недоступна или устроена не так, как мы ждём."""


def connect(path: str | Path) -> sqlite3.Connection:
    """Открывает базу дашборда только для чтения — испортить её нельзя."""
    file = Path(path)
    if not file.is_file():
        raise DashboardError(f"база дашборда не найдена: {file}")
    try:
        conn = sqlite3.connect(f"file:{file}?mode=ro", uri=True)
    except sqlite3.Error as error:
        raise DashboardError(f"не открывается {file}: {error}") from None
    conn.row_factory = sqlite3.Row

    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(reviews)")}
    except sqlite3.Error as error:
        conn.close()
        raise DashboardError(f"не читается таблица reviews: {error}") from None
    if not REQUIRED_COLUMNS <= columns:
        conn.close()
        missing = ", ".join(sorted(REQUIRED_COLUMNS - columns))
        raise DashboardError(f"это не похоже на базу reviews-dashboard (нет колонок: {missing})")
    return conn


def companies(path: str | Path) -> list[dict[str, Any]]:
    """Что вообще есть в базе дашборда — для выбора в кабинете."""
    conn = connect(path)
    try:
        rows = conn.execute(
            """SELECT company,
                      COUNT(*)    AS total,
                      AVG(rating) AS avg_rating,
                      MAX(date)   AS last_date,
                      GROUP_CONCAT(DISTINCT source) AS sources
               FROM reviews
               GROUP BY company
               ORDER BY total DESC"""
        ).fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        sources = sorted((row["sources"] or "").split(",")) if row["sources"] else []
        result.append({
            "company": row["company"],
            "total": int(row["total"]),
            "avg_rating": round(row["avg_rating"], 2) if row["avg_rating"] else None,
            "last_date": row["last_date"] or "",
            "sources": sources,
            "titles": [PLATFORMS.get(name, name) for name in sources],
        })
    return result


def read_rows(
    path: str | Path,
    company: str | None = None,
    source: str | None = None,
    min_rating: int = 1,
    since: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Отзывы из базы дашборда с фильтрами. Без оценки не берём — виджет
    показывает звёзды, а звёзды не из чего было бы рисовать."""
    sql = "SELECT * FROM reviews WHERE rating IS NOT NULL AND rating >= ?"
    params: list[Any] = [max(1, min(5, min_rating))]
    if company:
        sql += " AND company = ?"
        params.append(company)
    if source:
        sql += " AND source = ?"
        params.append(source)
    if since:
        sql += " AND date >= ?"
        params.append(since)
    sql += " ORDER BY date DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))

    conn = connect(path)
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def full_text(row: dict[str, Any]) -> str:
    """Отзыв маркетплейса состоит из «достоинств», «недостатков» и текста —
    склеиваем их так же, как это делает дашборд."""
    parts = []
    if row.get("pros"):
        parts.append(f"Достоинства: {row['pros']}")
    if row.get("cons"):
        parts.append(f"Недостатки: {row['cons']}")
    if row.get("text"):
        parts.append(str(row["text"]))
    return "\n".join(parts)


def to_review(site_key: str, row: dict[str, Any]) -> Review:
    """Строка дашборда → отзыв виджета. Валидацию делает Review.build."""
    review = Review.build(
        site=site_key,
        text=full_text(row),
        rating=row.get("rating"),
        author=row.get("author"),
        source=str(row.get("source") or "import"),
        meta={
            "dashboard_uid": clean_text(row.get("uid"), 32),
            "company": clean_text(row.get("company"), 120),
            "url": clean_text(row.get("url"), 300),
        },
    )
    review.reply = clean_text(row.get("reply"), 1000)
    date = clean_text(row.get("date"), 10)
    if date:
        review.created_at = date
    return review


def pull(
    storage: Storage,
    site: Site,
    path: str | Path,
    company: str | None = None,
    source: str | None = None,
    min_rating: int = 1,
    since: str | None = None,
    limit: int | None = None,
    publish: bool = False,
) -> dict[str, int]:
    """Забирает отзывы с площадок в базу виджета.

    По умолчанию всё падает в очередь модерации: даже если площадка отзыв
    опубликовала, решать, показывать ли его на своём сайте, должен владелец.
    Подозрительное не публикуется автоматически даже с `publish=True`.
    """
    rows = read_rows(path, company=company, source=source, min_rating=min_rating,
                     since=since, limit=limit)
    stats = {"seen": len(rows), "added": 0, "skipped": 0, "flagged": 0, "duplicates": 0}

    for row in rows:
        try:
            review = to_review(site.key, row)
        except ValidationError:
            # Слишком короткий отзыв или мусор в оценке — на сайте он не нужен.
            stats["skipped"] += 1
            continue

        verdict = check_text(review.text, review.author)
        if verdict.flags:
            review.meta["flags"] = verdict.flags
            stats["flagged"] += 1
        # Отзывы с площадок уже прошли чужую модерацию, поэтому спам-фильтр
        # здесь только помечает, а не выбрасывает.
        if publish and verdict.ok and not verdict.flags:
            review.status = "published"
            review.published_at = review.created_at

        if storage.add_review(review)[1]:
            stats["added"] += 1
        else:
            stats["duplicates"] += 1
    return stats


def platform_titles(sources: Iterable[str]) -> list[str]:
    return [PLATFORMS.get(name, name) for name in sources]


def dump_stats(stats: dict[str, int]) -> str:
    """Человекочитаемый итог импорта."""
    return json.dumps(stats, ensure_ascii=False)
