"""Состояние: что уже отправлено, какие фильтры выставлены, немного статистики.

SQLite, потому что состояние маленькое, а требование к нему одно и жёсткое —
пережить перезапуск контейнера. Без него после каждого рестарта в канал
прилетала бы вся лента заново.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import aiosqlite

from .models import Order
from .sources import ALL_SLUGS

log = logging.getLogger(__name__)

#: Сколько дней помним отправленные заказы. Ленты бирж отдают только свежее,
#: поэтому месяца с запасом хватает, чтобы старый заказ не приехал «новым».
RETENTION_DAYS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    uid           TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT NOT NULL,
    budget_min    INTEGER,
    budget_max    INTEGER,
    category      TEXT,
    first_seen_at TEXT NOT NULL,
    sent          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS orders_seen_idx ON orders (first_seen_at);
CREATE INDEX IF NOT EXISTS orders_sent_idx ON orders (sent, first_seen_at);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stats (
    day    TEXT NOT NULL,
    source TEXT NOT NULL,
    found  INTEGER NOT NULL DEFAULT 0,
    sent   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, source)
);

CREATE TABLE IF NOT EXISTS favorites (
    uid      TEXT PRIMARY KEY,
    title    TEXT NOT NULL,
    url      TEXT NOT NULL,
    added_at TEXT NOT NULL
);

-- id нужен, чтобы кнопки в `/categories` ссылались на постоянный
-- идентификатор. Порядок рубрик меняется после каждого опроса, и кнопка,
-- закодированная позицией в списке, переключала бы не то, что показывала.
CREATE TABLE IF NOT EXISTS categories_seen (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    category     TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE (source, category)
);
"""


@dataclass(slots=True)
class Filters:
    """Пользовательские настройки отбора. Меняются командами бота."""

    keywords: list[str] = field(default_factory=list)
    """Ключевые слова. Пустой список — пропускать всё подряд."""

    stopwords: list[str] = field(default_factory=list)
    min_budget: int = 0
    sources: list[str] = field(default_factory=lambda: list(ALL_SLUGS))
    categories: list[str] = field(default_factory=list)
    """Белый список рубрик. Пустой — без ограничения по рубрике."""

    skip_unknown_budget: bool = False
    """Отбрасывать ли заказы, где бюджет не указан.

    По умолчанию нет: RSS-ленты часто не содержат суммы вовсе, и включённый
    флаг вместе с порогом бюджета выкосил бы почти всю ленту.
    """

    paused: bool = False


_FILTER_DEFAULTS = Filters()


class Storage:
    def __init__(self, db_path: str) -> None:
        self._path = Path(db_path)
        self._db: aiosqlite.Connection | None = None
        # Поллер и обработчики команд бота живут в одном цикле событий и лезут
        # в базу параллельно; одно соединение на всех без блокировки даёт гонки.
        self._lock = asyncio.Lock()

    # ── жизненный цикл ────────────────────────────────────────────────────

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        # WAL — чтобы чтение статистики не блокировалось записью из поллера.
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> Storage:
        await self.open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Storage.open() не вызван")
        return self._db

    # ── дедупликация ──────────────────────────────────────────────────────

    async def select_new(self, orders: Sequence[Order]) -> list[Order]:
        """Оставить только те заказы, которых ещё не видели.

        Дубли внутри самой пачки тоже отсекаем: одна и та же вакансия попадает
        в ленту дважды чаще, чем хотелось бы.
        """
        if not orders:
            return []

        async with self._lock:
            known: set[str] = set()
            uids = [order.uid for order in orders]
            for chunk_start in range(0, len(uids), 500):
                chunk = uids[chunk_start : chunk_start + 500]
                placeholders = ",".join("?" * len(chunk))
                async with self._conn.execute(
                    f"SELECT uid FROM orders WHERE uid IN ({placeholders})", chunk
                ) as cursor:
                    known.update(row["uid"] for row in await cursor.fetchall())

        fresh: list[Order] = []
        seen_in_batch: set[str] = set()
        for order in orders:
            if order.uid in known or order.uid in seen_in_batch:
                continue
            seen_in_batch.add(order.uid)
            fresh.append(order)
        return fresh

    async def mark_seen(self, orders: Iterable[Order], *, sent: bool) -> None:
        rows = [
            (
                order.uid,
                order.source,
                order.title,
                order.url,
                order.budget_min,
                order.budget_max,
                order.category,
                datetime.now(UTC).isoformat(),
                1 if sent else 0,
            )
            for order in orders
        ]
        if not rows:
            return

        async with self._lock:
            await self._conn.executemany(
                """INSERT OR IGNORE INTO orders
                   (uid, source, title, url, budget_min, budget_max, category, first_seen_at, sent)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            await self._conn.commit()

    async def prune(self, days: int = RETENTION_DAYS) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        async with self._lock:
            cursor = await self._conn.execute(
                "DELETE FROM orders WHERE first_seen_at < ?", (cutoff,)
            )
            await self._conn.commit()
            return cursor.rowcount or 0

    async def count_orders(self) -> int:
        async with self._lock, self._conn.execute("SELECT COUNT(*) AS n FROM orders") as cursor:
            row = await cursor.fetchone()
            return int(row["n"]) if row else 0

    async def last_sent(self, limit: int = 5) -> list[aiosqlite.Row]:
        async with self._lock, self._conn.execute(
            "SELECT * FROM orders WHERE sent = 1 ORDER BY first_seen_at DESC LIMIT ?",
            (limit,),
        ) as cursor:
            return list(await cursor.fetchall())

    # ── настройки ─────────────────────────────────────────────────────────

    async def get_filters(self) -> Filters:
        async with self._lock, self._conn.execute("SELECT key, value FROM settings") as cursor:
            stored = {row["key"]: row["value"] for row in await cursor.fetchall()}

        values: dict[str, object] = {}
        for name in Filters.__dataclass_fields__:
            raw = stored.get(f"filter.{name}")
            if raw is None:
                continue
            try:
                values[name] = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("настройка %s повреждена, беру значение по умолчанию", name)
        return replace(_FILTER_DEFAULTS, **values)  # type: ignore[arg-type]

    async def save_filters(self, filters: Filters) -> None:
        rows = [
            (f"filter.{name}", json.dumps(getattr(filters, name), ensure_ascii=False))
            for name in Filters.__dataclass_fields__
        ]
        async with self._lock:
            await self._conn.executemany(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                rows,
            )
            await self._conn.commit()

    async def get_state(self, key: str, default: str | None = None) -> str | None:
        async with self._lock, self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", (f"state.{key}",)
        ) as cursor:
            row = await cursor.fetchone()
            return row["value"] if row else default

    async def set_state(self, key: str, value: str) -> None:
        async with self._lock:
            await self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (f"state.{key}", value),
            )
            await self._conn.commit()

    # ── статистика и рубрики ──────────────────────────────────────────────

    async def record_stats(self, source: str, *, found: int, sent: int) -> None:
        today = date.today().isoformat()
        async with self._lock:
            await self._conn.execute(
                """INSERT INTO stats (day, source, found, sent) VALUES (?, ?, ?, ?)
                   ON CONFLICT(day, source) DO UPDATE SET
                       found = found + excluded.found,
                       sent  = sent  + excluded.sent""",
                (today, source, found, sent),
            )
            await self._conn.commit()

    async def stats_since(self, days: int = 7) -> list[aiosqlite.Row]:
        since = (date.today() - timedelta(days=days)).isoformat()
        async with self._lock, self._conn.execute(
            """SELECT source, SUM(found) AS found, SUM(sent) AS sent
               FROM stats WHERE day >= ? GROUP BY source ORDER BY sent DESC""",
            (since,),
        ) as cursor:
            return list(await cursor.fetchall())

    async def note_categories(self, orders: Iterable[Order]) -> None:
        """Запомнить встреченные рубрики.

        Списки рубрик у бирж не совпадают и меняются, поэтому не зашиваем их в
        код, а накапливаем из реальной ленты — бот показывает в `/categories`
        именно то, что действительно приходит.
        """
        now = datetime.now(UTC).isoformat()
        rows = [
            (order.source, order.category, now)
            for order in orders
            if order.category and order.category.strip()
        ]
        if not rows:
            return

        async with self._lock:
            await self._conn.executemany(
                """INSERT INTO categories_seen (source, category, last_seen_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(source, category)
                   DO UPDATE SET last_seen_at = excluded.last_seen_at""",
                rows,
            )
            await self._conn.commit()

    async def known_categories(self, limit: int = 60) -> list[aiosqlite.Row]:
        async with self._lock, self._conn.execute(
            "SELECT id, source, category FROM categories_seen ORDER BY source, category LIMIT ?",
            (limit,),
        ) as cursor:
            return list(await cursor.fetchall())

    async def category_by_id(self, category_id: int) -> aiosqlite.Row | None:
        async with self._lock, self._conn.execute(
            "SELECT id, source, category FROM categories_seen WHERE id = ?", (category_id,)
        ) as cursor:
            return await cursor.fetchone()

    async def order_by_uid(self, uid: str) -> aiosqlite.Row | None:
        async with self._lock, self._conn.execute(
            "SELECT * FROM orders WHERE uid = ?", (uid,)
        ) as cursor:
            return await cursor.fetchone()

    # ── избранное ─────────────────────────────────────────────────────────

    async def add_favorite(self, uid: str, title: str, url: str) -> None:
        async with self._lock:
            await self._conn.execute(
                "INSERT OR REPLACE INTO favorites (uid, title, url, added_at) VALUES (?, ?, ?, ?)",
                (uid, title, url, datetime.now(UTC).isoformat()),
            )
            await self._conn.commit()

    async def list_favorites(self, limit: int = 20) -> list[aiosqlite.Row]:
        async with self._lock, self._conn.execute(
            "SELECT * FROM favorites ORDER BY added_at DESC LIMIT ?", (limit,)
        ) as cursor:
            return list(await cursor.fetchall())
