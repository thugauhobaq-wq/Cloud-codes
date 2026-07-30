"""Хранилище: магниты, подписчики, выдачи, рассылки.

База подписчиков — единственная ценность этого бота: сам файл легко перезалить,
а собранную аудиторию восстановить неоткуда. Отсюда WAL, отдельная таблица
выдач (а не счётчик) и мягкая пометка `blocked` вместо удаления строк.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from .codes import normalize
from .models import Broadcast, Delivery, Lead, Magnet

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS magnets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL UNIQUE,
    title      TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'text',
    body       TEXT NOT NULL DEFAULT '',
    file_id    TEXT NOT NULL DEFAULT '',
    followup   TEXT NOT NULL DEFAULT '',
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    tg_id      INTEGER PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    username   TEXT,
    source     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    -- Заблокировавших бота не удаляем: статистика воронки должна помнить, что
    -- человек в ней был, а рассылка — не пытаться писать ему снова.
    blocked    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS deliveries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id         INTEGER NOT NULL,
    magnet_id       INTEGER NOT NULL REFERENCES magnets (id) ON DELETE CASCADE,
    source          TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    followup_sent_at TEXT
);
CREATE INDEX IF NOT EXISTS deliveries_lead_idx ON deliveries (lead_id, magnet_id);
CREATE INDEX IF NOT EXISTS deliveries_time_idx ON deliveries (created_at);
CREATE INDEX IF NOT EXISTS deliveries_followup_idx ON deliveries (followup_sent_at, created_at);

CREATE TABLE IF NOT EXISTS broadcasts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT NOT NULL,
    magnet_code TEXT NOT NULL DEFAULT '',
    total       INTEGER NOT NULL DEFAULT 0,
    sent        INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class DuplicateCode(RuntimeError):
    """Кодовое слово уже занято другим магнитом."""


class Storage:
    def __init__(self, db_path: str) -> None:
        self._path = Path(db_path)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # ── жизненный цикл ────────────────────────────────────────────────────

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
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

    # ── магниты ───────────────────────────────────────────────────────────

    async def list_magnets(self, *, only_active: bool = True) -> list[Magnet]:
        sql = "SELECT * FROM magnets"
        if only_active:
            sql += " WHERE active = 1"
        sql += " ORDER BY id"
        async with self._lock, self._conn.execute(sql) as cursor:
            return [_magnet(row) for row in await cursor.fetchall()]

    async def get_magnet(self, magnet_id: int) -> Magnet | None:
        async with self._lock, self._conn.execute(
            "SELECT * FROM magnets WHERE id = ?", (magnet_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return _magnet(row) if row else None

    async def magnet_by_code(self, code: str, *, only_active: bool = True) -> Magnet | None:
        """Найти магнит по кодовому слову — с нормализацией пользовательского ввода."""
        normalized = normalize(code)
        if not normalized:
            return None
        sql = "SELECT * FROM magnets WHERE code = ?"
        if only_active:
            sql += " AND active = 1"
        async with self._lock, self._conn.execute(sql, (normalized,)) as cursor:
            row = await cursor.fetchone()
        return _magnet(row) if row else None

    async def add_magnet(
        self,
        code: str,
        title: str,
        *,
        kind: str = "text",
        body: str = "",
        file_id: str = "",
        followup: str = "",
    ) -> Magnet:
        normalized = normalize(code)
        if not normalized:
            raise ValueError("пустое кодовое слово")

        async with self._lock:
            async with self._conn.execute(
                "SELECT 1 FROM magnets WHERE code = ?", (normalized,)
            ) as cursor:
                if await cursor.fetchone():
                    raise DuplicateCode(normalized)

            cursor = await self._conn.execute(
                "INSERT INTO magnets (code, title, kind, body, file_id, followup, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (normalized, title, kind, body, file_id, followup, _now()),
            )
            await self._conn.commit()
            magnet_id = int(cursor.lastrowid)

        magnet = await self.get_magnet(magnet_id)
        assert magnet is not None
        return magnet

    async def update_magnet(self, magnet_id: int, **fields) -> None:
        allowed = {"code", "title", "kind", "body", "file_id", "followup", "active"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if "code" in updates:
            updates["code"] = normalize(str(updates["code"]))
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        async with self._lock:
            await self._conn.execute(
                f"UPDATE magnets SET {assignments} WHERE id = ?",
                (*updates.values(), magnet_id),
            )
            await self._conn.commit()

    async def delete_magnet(self, magnet_id: int) -> None:
        async with self._lock:
            await self._conn.execute("DELETE FROM magnets WHERE id = ?", (magnet_id,))
            await self._conn.commit()

    # ── подписчики ────────────────────────────────────────────────────────

    async def upsert_lead(
        self, tg_id: int, *, name: str = "", username: str | None = None, source: str = ""
    ) -> Lead:
        """Записать или обновить подписчика.

        Источник не перезаписываем: важно, откуда человек пришёл впервые, а не
        по какой ссылке он потом кликнул из любопытства.
        """
        async with self._lock:
            await self._conn.execute(
                "INSERT INTO leads (tg_id, name, username, source, created_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (tg_id) DO UPDATE SET"
                "   name = CASE WHEN excluded.name <> '' THEN excluded.name ELSE leads.name END,"
                "   username = excluded.username,"
                "   blocked = 0",
                (tg_id, name, username, source, _now()),
            )
            await self._conn.commit()

        lead = await self.get_lead(tg_id)
        assert lead is not None
        return lead

    async def get_lead(self, tg_id: int) -> Lead | None:
        async with self._lock, self._conn.execute(
            "SELECT * FROM leads WHERE tg_id = ?", (tg_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return _lead(row) if row else None

    async def mark_blocked(self, tg_id: int, blocked: bool = True) -> None:
        async with self._lock:
            await self._conn.execute(
                "UPDATE leads SET blocked = ? WHERE tg_id = ?", (int(blocked), tg_id)
            )
            await self._conn.commit()

    async def list_leads(
        self, *, magnet_code: str = "", include_blocked: bool = False, limit: int | None = None
    ) -> list[Lead]:
        """Подписчики; при `magnet_code` — только получившие этот магнит."""
        sql = "SELECT DISTINCT l.* FROM leads l"
        params: list = []
        conditions: list[str] = []

        if magnet_code:
            sql += (
                " JOIN deliveries d ON d.lead_id = l.tg_id"
                " JOIN magnets m ON m.id = d.magnet_id"
            )
            conditions.append("m.code = ?")
            params.append(normalize(magnet_code))
        if not include_blocked:
            conditions.append("l.blocked = 0")
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY l.created_at"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        async with self._lock, self._conn.execute(sql, params) as cursor:
            return [_lead(row) for row in await cursor.fetchall()]

    async def count_leads(self, *, include_blocked: bool = False) -> int:
        sql = "SELECT COUNT(*) FROM leads"
        if not include_blocked:
            sql += " WHERE blocked = 0"
        async with self._lock, self._conn.execute(sql) as cursor:
            return int((await cursor.fetchone())[0])

    # ── выдачи ────────────────────────────────────────────────────────────

    async def record_delivery(self, lead_id: int, magnet_id: int, source: str = "") -> Delivery:
        async with self._lock:
            cursor = await self._conn.execute(
                "INSERT INTO deliveries (lead_id, magnet_id, source, created_at)"
                " VALUES (?, ?, ?, ?)",
                (lead_id, magnet_id, source, _now()),
            )
            await self._conn.commit()
            delivery_id = int(cursor.lastrowid)

        async with self._lock, self._conn.execute(
            _DELIVERY_SELECT + " WHERE d.id = ?", (delivery_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return _delivery(row)

    async def already_delivered(self, lead_id: int, magnet_id: int) -> bool:
        async with self._lock, self._conn.execute(
            "SELECT 1 FROM deliveries WHERE lead_id = ? AND magnet_id = ? LIMIT 1",
            (lead_id, magnet_id),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def lead_deliveries(self, lead_id: int) -> list[Delivery]:
        async with self._lock, self._conn.execute(
            _DELIVERY_SELECT + " WHERE d.lead_id = ? ORDER BY d.created_at", (lead_id,)
        ) as cursor:
            return [_delivery(row) for row in await cursor.fetchall()]

    async def due_followups(self, now: datetime, delay: timedelta) -> list[Delivery]:
        """Выдачи, по которым пора отправить догоняющее сообщение.

        Только те магниты, у которых текст догона задан: пустой followup —
        это осознанный отказ от второго касания, а не недоделка.
        """
        async with self._lock, self._conn.execute(
            _DELIVERY_SELECT + " WHERE d.followup_sent_at IS NULL AND d.created_at <= ?"
            "   AND m.followup <> '' AND m.active = 1"
            " ORDER BY d.created_at LIMIT 200",
            ((now - delay).isoformat(),),
        ) as cursor:
            return [_delivery(row) for row in await cursor.fetchall()]

    async def mark_followup_sent(self, delivery_id: int) -> None:
        async with self._lock:
            await self._conn.execute(
                "UPDATE deliveries SET followup_sent_at = ? WHERE id = ?", (_now(), delivery_id)
            )
            await self._conn.commit()

    # ── рассылки ──────────────────────────────────────────────────────────

    async def create_broadcast(self, text: str, magnet_code: str = "", total: int = 0) -> Broadcast:
        async with self._lock:
            cursor = await self._conn.execute(
                "INSERT INTO broadcasts (text, magnet_code, total, created_at)"
                " VALUES (?, ?, ?, ?)",
                (text, magnet_code, total, _now()),
            )
            await self._conn.commit()
            broadcast_id = int(cursor.lastrowid)

        broadcast = await self.get_broadcast(broadcast_id)
        assert broadcast is not None
        return broadcast

    async def get_broadcast(self, broadcast_id: int) -> Broadcast | None:
        async with self._lock, self._conn.execute(
            "SELECT * FROM broadcasts WHERE id = ?", (broadcast_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return _broadcast(row) if row else None

    async def finish_broadcast(self, broadcast_id: int, sent: int, failed: int) -> None:
        async with self._lock:
            await self._conn.execute(
                "UPDATE broadcasts SET sent = ?, failed = ?, status = 'done' WHERE id = ?",
                (sent, failed, broadcast_id),
            )
            await self._conn.commit()

    async def last_broadcasts(self, limit: int = 5) -> list[Broadcast]:
        async with self._lock, self._conn.execute(
            "SELECT * FROM broadcasts ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cursor:
            return [_broadcast(row) for row in await cursor.fetchall()]

    # ── статистика ────────────────────────────────────────────────────────

    async def stats(self, days: int = 30) -> dict[str, int]:
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        async with self._lock:
            async with self._conn.execute(
                "SELECT COUNT(*) FROM leads WHERE created_at >= ?", (since,)
            ) as cursor:
                new_leads = int((await cursor.fetchone())[0])
            async with self._conn.execute(
                "SELECT COUNT(*) FROM deliveries WHERE created_at >= ?", (since,)
            ) as cursor:
                deliveries = int((await cursor.fetchone())[0])
            async with self._conn.execute("SELECT COUNT(*) FROM leads WHERE blocked = 1") as cursor:
                blocked = int((await cursor.fetchone())[0])
            async with self._conn.execute("SELECT COUNT(*) FROM leads") as cursor:
                total = int((await cursor.fetchone())[0])
        return {
            "total_leads": total,
            "new_leads": new_leads,
            "deliveries": deliveries,
            "blocked": blocked,
        }

    async def by_magnet(self, days: int = 30) -> list[tuple[str, str, int]]:
        """(код, название, число выдач) по убыванию."""
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        async with self._lock, self._conn.execute(
            "SELECT m.code AS code, m.title AS title, COUNT(d.id) AS total"
            " FROM magnets m LEFT JOIN deliveries d"
            "   ON d.magnet_id = m.id AND d.created_at >= ?"
            " GROUP BY m.id ORDER BY total DESC, m.id",
            (since,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [(row["code"], row["title"], int(row["total"])) for row in rows]

    async def by_source(self, days: int = 30, limit: int = 10) -> list[tuple[str, int]]:
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        async with self._lock, self._conn.execute(
            "SELECT CASE WHEN source = '' THEN 'без метки' ELSE source END AS label,"
            "       COUNT(*) AS total"
            " FROM deliveries WHERE created_at >= ?"
            " GROUP BY label ORDER BY total DESC LIMIT ?",
            (since, limit),
        ) as cursor:
            return [(row["label"], int(row["total"])) for row in await cursor.fetchall()]

    async def daily(self, days: int = 7) -> list[tuple[str, int]]:
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        async with self._lock, self._conn.execute(
            "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS total"
            " FROM deliveries WHERE created_at >= ? GROUP BY day ORDER BY day",
            (since,),
        ) as cursor:
            return [(row["day"], int(row["total"])) for row in await cursor.fetchall()]

    async def export_rows(self) -> Sequence[tuple]:
        """Строки для выгрузки базы: подписчик и его магниты."""
        async with self._lock, self._conn.execute(
            "SELECT l.tg_id, l.name, l.username, l.source, l.created_at, l.blocked,"
            "       COALESCE(GROUP_CONCAT(m.code, ', '), '') AS codes"
            " FROM leads l"
            " LEFT JOIN deliveries d ON d.lead_id = l.tg_id"
            " LEFT JOIN magnets m ON m.id = d.magnet_id"
            " GROUP BY l.tg_id ORDER BY l.created_at"
        ) as cursor:
            return [tuple(row) for row in await cursor.fetchall()]


# ──────────────────────────────────────────────────────────────────────────────
# Преобразование строк
# ──────────────────────────────────────────────────────────────────────────────

_DELIVERY_SELECT = """
SELECT d.*, m.code AS magnet_code, m.title AS magnet_title
FROM deliveries d
JOIN magnets m ON m.id = d.magnet_id
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _dt(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


def _magnet(row: aiosqlite.Row) -> Magnet:
    return Magnet(
        id=int(row["id"]),
        code=row["code"],
        title=row["title"],
        kind=row["kind"],
        body=row["body"],
        file_id=row["file_id"],
        followup=row["followup"],
        active=bool(row["active"]),
        created_at=_dt(row["created_at"]),
    )


def _lead(row: aiosqlite.Row) -> Lead:
    return Lead(
        tg_id=int(row["tg_id"]),
        name=row["name"],
        username=row["username"],
        source=row["source"],
        created_at=_dt(row["created_at"]),
        blocked=bool(row["blocked"]),
    )


def _delivery(row: aiosqlite.Row) -> Delivery:
    return Delivery(
        id=int(row["id"]),
        lead_id=int(row["lead_id"]),
        magnet_id=int(row["magnet_id"]),
        created_at=_dt(row["created_at"]),
        followup_sent_at=_dt(row["followup_sent_at"]),
        source=row["source"],
        magnet_code=row["magnet_code"],
        magnet_title=row["magnet_title"],
    )


def _broadcast(row: aiosqlite.Row) -> Broadcast:
    return Broadcast(
        id=int(row["id"]),
        text=row["text"],
        magnet_code=row["magnet_code"],
        total=int(row["total"]),
        sent=int(row["sent"]),
        failed=int(row["failed"]),
        status=row["status"],
        created_at=_dt(row["created_at"]),
    )
