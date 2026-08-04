"""Хранилище на SQLite: сайты и отзывы. Без внешних зависимостей."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from .models import DEFAULT_SETTINGS, Review, Site, normalize_settings, now_iso

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "widget.db"

#: Базы, схему которых этот процесс уже проверил. Сервер открывает соединение
#: на каждый запрос, и прогонять DDL каждый раз незачем: это около 0.1 мс из
#: примерно 2 мс на запрос — немного, но и не бесплатно, а миграции тем более
#: должны выполняться однажды.
_READY: set[str] = set()
_READY_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    key               TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    token_hash        TEXT NOT NULL DEFAULT '',
    domains           TEXT NOT NULL DEFAULT '[]',
    auto_approve      INTEGER NOT NULL DEFAULT 0,
    auto_approve_from INTEGER NOT NULL DEFAULT 5,
    telegram_token    TEXT NOT NULL DEFAULT '',
    telegram_chat     TEXT NOT NULL DEFAULT '',
    settings          TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    site         TEXT NOT NULL,
    uid          TEXT NOT NULL,
    author       TEXT NOT NULL DEFAULT 'Аноним',
    rating       INTEGER NOT NULL,
    text         TEXT NOT NULL,
    contact      TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT 'form',
    status       TEXT NOT NULL DEFAULT 'pending',
    reply        TEXT NOT NULL DEFAULT '',
    pinned       INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    published_at TEXT NOT NULL DEFAULT '',
    meta         TEXT NOT NULL DEFAULT '{}',
    UNIQUE(site, uid)
);

CREATE INDEX IF NOT EXISTS idx_reviews_site   ON reviews(site, status);
CREATE INDEX IF NOT EXISTS idx_reviews_date   ON reviews(site, created_at);
"""

#: Пересборка таблицы сайтов при переходе с открытого токена на хеш. Просто
#: добавить колонку нельзя: у старого `admin_token` было NOT NULL без значения
#: по умолчанию, и новые сайты в такую таблицу уже не вставились бы.
REBUILD_SITES = """
CREATE TABLE sites_new (
    key               TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    token_hash        TEXT NOT NULL DEFAULT '',
    domains           TEXT NOT NULL DEFAULT '[]',
    auto_approve      INTEGER NOT NULL DEFAULT 0,
    auto_approve_from INTEGER NOT NULL DEFAULT 5,
    telegram_token    TEXT NOT NULL DEFAULT '',
    telegram_chat     TEXT NOT NULL DEFAULT '',
    settings          TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL
);
INSERT INTO sites_new (key, name, token_hash, domains, auto_approve,
                       auto_approve_from, telegram_token, telegram_chat,
                       settings, created_at)
SELECT key, name, admin_token, domains, auto_approve, auto_approve_from,
       telegram_token, telegram_chat, settings, created_at
FROM sites;
DROP TABLE sites;
ALTER TABLE sites_new RENAME TO sites;
"""


def _row_to_site(row: sqlite3.Row) -> Site:
    return Site(
        key=row["key"],
        name=row["name"],
        token_hash=row["token_hash"],
        domains=json.loads(row["domains"] or "[]"),
        auto_approve=bool(row["auto_approve"]),
        auto_approve_from=int(row["auto_approve_from"]),
        telegram_token=row["telegram_token"],
        telegram_chat=row["telegram_chat"],
        settings=normalize_settings(row["settings"]),
        created_at=row["created_at"],
    )


def _row_to_review(row: sqlite3.Row) -> Review:
    return Review(
        id=row["id"],
        site=row["site"],
        uid=row["uid"],
        author=row["author"],
        rating=int(row["rating"]),
        text=row["text"],
        contact=row["contact"],
        source=row["source"],
        status=row["status"],
        reply=row["reply"],
        pinned=bool(row["pinned"]),
        created_at=row["created_at"],
        published_at=row["published_at"],
        meta=json.loads(row["meta"] or "{}"),
    )


class Storage:
    def __init__(self, path: str | Path = DEFAULT_DB, migrate: bool | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # Ждать освободившуюся блокировку, а не падать: писать могут сразу
        # сервер, бот и импорт из консоли. Столько же ставит и sqlite3 по
        # умолчанию — здесь это записано явно, чтобы не зависеть от версии.
        self._conn.execute("PRAGMA busy_timeout = 5000")

        key = str(self.path.resolve())
        if migrate is None:
            migrate = key not in _READY
        if migrate:
            with _READY_LOCK:
                self._prepare()
                _READY.add(key)

    def _prepare(self) -> None:
        """Создаёт схему и доводит старые базы до текущей. Один раз на процесс."""
        try:
            # WAL: читатель не блокирует писателя. На нагрузке, которую удалось
            # воспроизвести, разницы в скорости нет — берём его не за скорость,
            # а чтобы отдача отзывов не могла встать в очередь за импортом или
            # приёмом формы. Режим запоминается в файле базы, поэтому ставим
            # его только при подготовке.
            self._conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.Error:
            # Сетевые и read-only ФС WAL не поддерживают — работаем как раньше.
            pass
        self._conn.executescript(SCHEMA)
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(sites)")}
        if "token_hash" not in columns and "admin_token" in columns:
            self._conn.executescript(REBUILD_SITES)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- сайты

    def add_site(self, site: Site) -> Site:
        self._conn.execute(
            """INSERT INTO sites (key, name, token_hash, domains, auto_approve,
                                  auto_approve_from, telegram_token, telegram_chat,
                                  settings, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                site.key, site.name, site.token_hash, json.dumps(site.domains, ensure_ascii=False),
                int(site.auto_approve), site.auto_approve_from, site.telegram_token,
                site.telegram_chat, json.dumps(site.settings, ensure_ascii=False), site.created_at,
            ),
        )
        self._conn.commit()
        return site

    def get_site(self, key: str) -> Site | None:
        row = self._conn.execute("SELECT * FROM sites WHERE key = ?", (key,)).fetchone()
        return _row_to_site(row) if row else None

    def list_sites(self) -> list[Site]:
        rows = self._conn.execute("SELECT * FROM sites ORDER BY created_at").fetchall()
        return [_row_to_site(row) for row in rows]

    def sites_with_bot(self) -> list[Site]:
        rows = self._conn.execute(
            "SELECT * FROM sites WHERE telegram_token != '' ORDER BY created_at"
        ).fetchall()
        return [_row_to_site(row) for row in rows]

    def update_site(self, site_key: str, **fields: Any) -> Site | None:
        site = self.get_site(site_key)
        if site is None:
            return None
        if "settings" in fields:
            fields["settings"] = json.dumps(
                normalize_settings(fields["settings"], site.settings), ensure_ascii=False
            )
        if "domains" in fields:
            fields["domains"] = json.dumps(fields["domains"], ensure_ascii=False)
        if "auto_approve" in fields:
            fields["auto_approve"] = int(bool(fields["auto_approve"]))

        allowed = {
            "name", "token_hash", "domains", "auto_approve", "auto_approve_from",
            "telegram_token", "telegram_chat", "settings",
        }
        pairs = {k: v for k, v in fields.items() if k in allowed}
        if pairs:
            assignments = ", ".join(f"{k} = ?" for k in pairs)
            self._conn.execute(
                f"UPDATE sites SET {assignments} WHERE key = ?", (*pairs.values(), site_key)
            )
            self._conn.commit()
        return self.get_site(site_key)

    def delete_site(self, key: str) -> None:
        self._conn.execute("DELETE FROM reviews WHERE site = ?", (key,))
        self._conn.execute("DELETE FROM sites WHERE key = ?", (key,))
        self._conn.commit()

    # --------------------------------------------------------------- отзывы

    def add_review(self, review: Review) -> tuple[Review, bool]:
        """Возвращает (отзыв, создан_ли). Повтор того же текста не дублируется."""
        existing = self._conn.execute(
            "SELECT * FROM reviews WHERE site = ? AND uid = ?", (review.site, review.uid)
        ).fetchone()
        if existing:
            return _row_to_review(existing), False

        if review.status == "published" and not review.published_at:
            review.published_at = now_iso()
        cursor = self._conn.execute(
            """INSERT INTO reviews (site, uid, author, rating, text, contact, source,
                                    status, reply, pinned, created_at, published_at, meta)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                review.site, review.uid, review.author, review.rating, review.text,
                review.contact, review.source, review.status, review.reply, int(review.pinned),
                review.created_at, review.published_at,
                json.dumps(review.meta, ensure_ascii=False),
            ),
        )
        self._conn.commit()
        review.id = int(cursor.lastrowid)
        return review, True

    def add_many(self, reviews: Iterable[Review]) -> int:
        return sum(1 for review in reviews if self.add_review(review)[1])

    def get_review(self, review_id: int) -> Review | None:
        row = self._conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
        return _row_to_review(row) if row else None

    def published(self, site: str, limit: int = 20, min_rating: int = 1) -> list[Review]:
        """Что показывает виджет: сначала закреплённые, дальше свежие."""
        rows = self._conn.execute(
            """SELECT * FROM reviews
               WHERE site = ? AND status = 'published' AND rating >= ?
               ORDER BY pinned DESC,
                        CASE WHEN published_at = '' THEN created_at ELSE published_at END DESC
               LIMIT ?""",
            (site, min_rating, max(1, limit)),
        ).fetchall()
        return [_row_to_review(row) for row in rows]

    def list_reviews(
        self, site: str, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[Review]:
        query = "SELECT * FROM reviews WHERE site = ?"
        params: list[Any] = [site]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY pinned DESC, created_at DESC LIMIT ? OFFSET ?"
        params += [max(1, limit), max(0, offset)]
        return [_row_to_review(row) for row in self._conn.execute(query, params).fetchall()]

    def counts(self, site: str) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM reviews WHERE site = ? GROUP BY status", (site,)
        ).fetchall()
        result = {status: 0 for status in ("pending", "published", "rejected")}
        for row in rows:
            result[row["status"]] = int(row["n"])
        result["total"] = sum(result.values())
        return result

    def stats(self, site: str, min_rating: int = 1) -> dict[str, Any]:
        """Средняя оценка и разбивка по звёздам — для шапки виджета."""
        rows = self._conn.execute(
            """SELECT rating, COUNT(*) AS n FROM reviews
               WHERE site = ? AND status = 'published' AND rating >= ?
               GROUP BY rating""",
            (site, min_rating),
        ).fetchall()
        distribution = {str(star): 0 for star in range(1, 6)}
        total = 0
        weighted = 0
        for row in rows:
            count = int(row["n"])
            distribution[str(int(row["rating"]))] = count
            total += count
            weighted += count * int(row["rating"])
        average = round(weighted / total, 2) if total else 0.0
        return {"count": total, "average": average, "distribution": distribution}

    def set_status(self, review_id: int, status: str) -> Review | None:
        published_at = now_iso() if status == "published" else ""
        self._conn.execute(
            "UPDATE reviews SET status = ?, published_at = ? WHERE id = ?",
            (status, published_at, review_id),
        )
        self._conn.commit()
        return self.get_review(review_id)

    def set_reply(self, review_id: int, reply: str) -> Review | None:
        self._conn.execute("UPDATE reviews SET reply = ? WHERE id = ?", (reply, review_id))
        self._conn.commit()
        return self.get_review(review_id)

    def set_pinned(self, review_id: int, pinned: bool) -> Review | None:
        self._conn.execute(
            "UPDATE reviews SET pinned = ? WHERE id = ?", (int(pinned), review_id)
        )
        self._conn.commit()
        return self.get_review(review_id)

    def delete_review(self, review_id: int) -> None:
        self._conn.execute("DELETE FROM reviews WHERE id = ?", (review_id,))
        self._conn.commit()

    def count_since(self, site: str, since_iso: str) -> int:
        """Сколько отзывов пришло с указанного момента — для антифлуда."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM reviews WHERE site = ? AND created_at >= ?",
            (site, since_iso),
        ).fetchone()
        return int(row["n"])


def ensure_settings(site: Site) -> dict[str, Any]:
    """Настройки сайта, дополненные значениями по умолчанию."""
    return {**DEFAULT_SETTINGS, **site.settings}
