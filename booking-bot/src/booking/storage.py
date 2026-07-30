"""Хранилище: услуги, мастера, расписание, записи, клиенты.

SQLite — состояние маленькое (одна точка, десятки записей в день), а требование
к нему одно: пережить перезапуск контейнера, не потеряв ничью запись.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from .models import (
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_DONE,
    Booking,
    Client,
    Interval,
    Master,
    Service,
    WorkWindow,
)

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS services (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    duration_min INTEGER NOT NULL,
    price        INTEGER,
    description  TEXT NOT NULL DEFAULT '',
    active       INTEGER NOT NULL DEFAULT 1,
    position     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS masters (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name   TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

-- master_id = NULL означает общее расписание точки; личное расписание мастера
-- перекрывает его целиком (см. schedule.windows_for).
CREATE TABLE IF NOT EXISTS work_hours (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    master_id INTEGER REFERENCES masters (id) ON DELETE CASCADE,
    weekday   INTEGER NOT NULL,
    start_min INTEGER NOT NULL,
    end_min   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS timeoff (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    master_id INTEGER REFERENCES masters (id) ON DELETE CASCADE,
    starts_at TEXT NOT NULL,
    ends_at   TEXT NOT NULL,
    note      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS clients (
    tg_id      INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    phone      TEXT NOT NULL DEFAULT '',
    username   TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id    INTEGER NOT NULL,
    service_id   INTEGER NOT NULL,
    master_id    INTEGER,
    starts_at    TEXT NOT NULL,
    ends_at      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active',
    comment      TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    reminded_at  TEXT,
    cancelled_by TEXT
);
CREATE INDEX IF NOT EXISTS bookings_time_idx ON bookings (starts_at, status);
CREATE INDEX IF NOT EXISTS bookings_client_idx ON bookings (client_id, status);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class SlotTaken(RuntimeError):
    """Слот заняли, пока клиент выбирал. Обрабатывается в хендлере записи."""


class Storage:
    def __init__(self, db_path: str) -> None:
        self._path = Path(db_path)
        self._db: aiosqlite.Connection | None = None
        # Хендлеры бота и фоновый напоминатель ходят в базу из одного цикла
        # событий; без блокировки на общем соединении получаются гонки.
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

    # ── услуги ────────────────────────────────────────────────────────────

    async def list_services(self, *, only_active: bool = True) -> list[Service]:
        sql = "SELECT * FROM services"
        if only_active:
            sql += " WHERE active = 1"
        sql += " ORDER BY position, id"
        async with self._lock, self._conn.execute(sql) as cursor:
            return [_service(row) for row in await cursor.fetchall()]

    async def get_service(self, service_id: int) -> Service | None:
        async with self._lock, self._conn.execute(
            "SELECT * FROM services WHERE id = ?", (service_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return _service(row) if row else None

    async def add_service(
        self,
        title: str,
        duration_min: int,
        price: int | None = None,
        description: str = "",
    ) -> int:
        async with self._lock:
            async with self._conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM services"
            ) as cursor:
                position = (await cursor.fetchone())[0]
            cursor = await self._conn.execute(
                "INSERT INTO services (title, duration_min, price, description, position)"
                " VALUES (?, ?, ?, ?, ?)",
                (title, duration_min, price, description, position),
            )
            await self._conn.commit()
            return int(cursor.lastrowid)

    async def update_service(self, service_id: int, **fields) -> None:
        allowed = {"title", "duration_min", "price", "description", "active", "position"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        async with self._lock:
            await self._conn.execute(
                f"UPDATE services SET {assignments} WHERE id = ?",
                (*updates.values(), service_id),
            )
            await self._conn.commit()

    async def delete_service(self, service_id: int) -> None:
        """Услугу не удаляем физически — на неё ссылаются прошлые записи.

        Иначе в истории и статистике появятся строки без названия.
        """
        await self.update_service(service_id, active=0)

    # ── мастера ───────────────────────────────────────────────────────────

    async def list_masters(self, *, only_active: bool = True) -> list[Master]:
        sql = "SELECT * FROM masters"
        if only_active:
            sql += " WHERE active = 1"
        sql += " ORDER BY id"
        async with self._lock, self._conn.execute(sql) as cursor:
            return [_master(row) for row in await cursor.fetchall()]

    async def get_master(self, master_id: int) -> Master | None:
        async with self._lock, self._conn.execute(
            "SELECT * FROM masters WHERE id = ?", (master_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return _master(row) if row else None

    async def add_master(self, name: str) -> int:
        async with self._lock:
            cursor = await self._conn.execute("INSERT INTO masters (name) VALUES (?)", (name,))
            await self._conn.commit()
            return int(cursor.lastrowid)

    async def set_master_active(self, master_id: int, active: bool) -> None:
        async with self._lock:
            await self._conn.execute(
                "UPDATE masters SET active = ? WHERE id = ?", (int(active), master_id)
            )
            await self._conn.commit()

    # ── расписание ────────────────────────────────────────────────────────

    async def list_windows(self, master_id: int | None = None) -> list[WorkWindow]:
        """Все окна — и общие, и личные.

        Отбор по мастеру делает `schedule.windows_for`: ему нужны обе группы
        сразу, чтобы решить, какая из них действует.
        """
        sql = "SELECT * FROM work_hours"
        params: tuple = ()
        if master_id is not None:
            sql += " WHERE master_id IS NULL OR master_id = ?"
            params = (master_id,)
        sql += " ORDER BY weekday, start_min"
        async with self._lock, self._conn.execute(sql, params) as cursor:
            return [_window(row) for row in await cursor.fetchall()]

    async def set_windows(
        self,
        weekdays: Iterable[int],
        windows: Sequence[tuple[int, int]],
        master_id: int | None = None,
    ) -> None:
        """Переписать расписание на перечисленные дни недели.

        Именно переписать, а не дополнить: «пн-пт 10:00-20:00» должно заменять
        то, что было, иначе повторная команда наплодит дубликатов окон.
        """
        days = sorted(set(weekdays))
        if not days:
            return

        placeholders = ",".join("?" * len(days))
        owner = "master_id IS ?" if master_id is None else "master_id = ?"
        async with self._lock:
            await self._conn.execute(
                f"DELETE FROM work_hours WHERE weekday IN ({placeholders}) AND {owner}",
                (*days, master_id),
            )
            await self._conn.executemany(
                "INSERT INTO work_hours (master_id, weekday, start_min, end_min)"
                " VALUES (?, ?, ?, ?)",
                [
                    (master_id, day, start, end)
                    for day in days
                    for start, end in windows
                    if start < end
                ],
            )
            await self._conn.commit()

    # ── выходные и отпуска ────────────────────────────────────────────────

    async def add_timeoff(
        self,
        starts_at: datetime,
        ends_at: datetime,
        master_id: int | None = None,
        note: str = "",
    ) -> int:
        async with self._lock:
            cursor = await self._conn.execute(
                "INSERT INTO timeoff (master_id, starts_at, ends_at, note) VALUES (?, ?, ?, ?)",
                (master_id, _iso(starts_at), _iso(ends_at), note),
            )
            await self._conn.commit()
            return int(cursor.lastrowid)

    async def list_timeoff(
        self, since: datetime | None = None
    ) -> list[tuple[int, Interval, int | None, str]]:
        sql = "SELECT * FROM timeoff"
        params: tuple = ()
        if since is not None:
            sql += " WHERE ends_at >= ?"
            params = (_iso(since),)
        sql += " ORDER BY starts_at"
        async with self._lock, self._conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [
            (
                int(row["id"]),
                Interval(_dt(row["starts_at"]), _dt(row["ends_at"])),
                row["master_id"],
                row["note"],
            )
            for row in rows
        ]

    async def delete_timeoff(self, timeoff_id: int) -> None:
        async with self._lock:
            await self._conn.execute("DELETE FROM timeoff WHERE id = ?", (timeoff_id,))
            await self._conn.commit()

    # ── занятость ─────────────────────────────────────────────────────────

    async def busy_intervals(
        self, start: datetime, end: datetime, master_id: int | None = None
    ) -> list[Interval]:
        """Всё, что мешает записаться в промежутке: чужие записи и отпуска.

        Когда мастер выбран, его занятость складывается из личных записей и
        отпусков — общих (`master_id IS NULL`) и своих. Когда мастера нет
        вовсе, точка одна и занято всё, что занято хоть кем-то.
        """
        booking_filter = "" if master_id is None else " AND (master_id = ? OR master_id IS NULL)"
        params: list = [_iso(end), _iso(start)]
        if master_id is not None:
            params.append(master_id)

        async with self._lock:
            async with self._conn.execute(
                "SELECT starts_at, ends_at FROM bookings"
                " WHERE status = 'active' AND starts_at < ? AND ends_at > ?" + booking_filter,
                params,
            ) as cursor:
                rows = list(await cursor.fetchall())

            off_filter = "" if master_id is None else " AND (master_id = ? OR master_id IS NULL)"
            off_params: list = [_iso(end), _iso(start)]
            if master_id is not None:
                off_params.append(master_id)
            async with self._conn.execute(
                "SELECT starts_at, ends_at FROM timeoff WHERE starts_at < ? AND ends_at > ?"
                + off_filter,
                off_params,
            ) as cursor:
                rows.extend(await cursor.fetchall())

        return [Interval(_dt(row["starts_at"]), _dt(row["ends_at"])) for row in rows]

    # ── клиенты ───────────────────────────────────────────────────────────

    async def upsert_client(
        self, tg_id: int, name: str, phone: str = "", username: str | None = None
    ) -> None:
        async with self._lock:
            await self._conn.execute(
                "INSERT INTO clients (tg_id, name, phone, username, created_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (tg_id) DO UPDATE SET"
                # Пустым телефоном не затираем сохранённый: клиент мог просто
                # не поделиться контактом при повторной записи.
                "   name = excluded.name,"
                "   phone = CASE WHEN excluded.phone <> ''"
                "        THEN excluded.phone ELSE clients.phone END,"
                "   username = excluded.username",
                (tg_id, name, phone, username, _iso(datetime.now(UTC))),
            )
            await self._conn.commit()

    async def get_client(self, tg_id: int) -> Client | None:
        async with self._lock, self._conn.execute(
            "SELECT * FROM clients WHERE tg_id = ?", (tg_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return Client(int(row["tg_id"]), row["name"], row["phone"], row["username"])

    async def count_clients(self) -> int:
        async with self._lock, self._conn.execute("SELECT COUNT(*) FROM clients") as cursor:
            return int((await cursor.fetchone())[0])

    # ── записи ────────────────────────────────────────────────────────────

    async def create_booking(
        self,
        *,
        client_id: int,
        service: Service,
        starts_at: datetime,
        master_id: int | None = None,
        comment: str = "",
    ) -> Booking:
        """Создать запись, если слот всё ещё свободен.

        Проверка занятости и вставка идут одной транзакцией: между показом
        сетки слотов и нажатием кнопки проходят минуты, и за это время слот
        успевают занять. Без этого двое клиентов встречаются в кресле.
        """
        ends_at = starts_at + service.duration
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                conflict_filter = (
                    "" if master_id is None else " AND (master_id = ? OR master_id IS NULL)"
                )
                params: list = [_iso(ends_at), _iso(starts_at)]
                if master_id is not None:
                    params.append(master_id)
                async with self._conn.execute(
                    "SELECT 1 FROM bookings"
                    " WHERE status = 'active' AND starts_at < ? AND ends_at > ?" + conflict_filter,
                    params,
                ) as cursor:
                    if await cursor.fetchone():
                        raise SlotTaken(starts_at.isoformat())

                created = datetime.now(UTC)
                cursor = await self._conn.execute(
                    "INSERT INTO bookings"
                    " (client_id, service_id, master_id, starts_at, ends_at, comment, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        client_id,
                        service.id,
                        master_id,
                        _iso(starts_at),
                        _iso(ends_at),
                        comment,
                        _iso(created),
                    ),
                )
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise

        booking = await self.get_booking(int(cursor.lastrowid))
        assert booking is not None
        return booking

    async def get_booking(self, booking_id: int) -> Booking | None:
        async with self._lock, self._conn.execute(
            _BOOKING_SELECT + " WHERE b.id = ?", (booking_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return _booking(row) if row else None

    async def client_bookings(
        self, client_id: int, *, only_active: bool = True, limit: int = 20
    ) -> list[Booking]:
        sql = _BOOKING_SELECT + " WHERE b.client_id = ?"
        params: list = [client_id]
        if only_active:
            sql += " AND b.status = 'active' AND b.starts_at >= ?"
            params.append(_iso(datetime.now(UTC)))
        sql += " ORDER BY b.starts_at LIMIT ?"
        params.append(limit)
        async with self._lock, self._conn.execute(sql, params) as cursor:
            return [_booking(row) for row in await cursor.fetchall()]

    async def bookings_between(
        self, start: datetime, end: datetime, *, only_active: bool = True
    ) -> list[Booking]:
        sql = _BOOKING_SELECT + " WHERE b.starts_at >= ? AND b.starts_at < ?"
        if only_active:
            sql += " AND b.status = 'active'"
        sql += " ORDER BY b.starts_at"
        async with self._lock, self._conn.execute(sql, (_iso(start), _iso(end))) as cursor:
            return [_booking(row) for row in await cursor.fetchall()]

    async def cancel_booking(self, booking_id: int, *, by: str) -> Booking | None:
        """Отменить запись. Возвращает её, если отмена состоялась.

        None означает «отменять было нечего» — например, клиент нажал кнопку
        под старым сообщением дважды, и второе нажатие уже ничего не меняет.
        """
        async with self._lock:
            cursor = await self._conn.execute(
                "UPDATE bookings SET status = ?, cancelled_by = ?"
                " WHERE id = ? AND status = 'active'",
                (STATUS_CANCELLED, by, booking_id),
            )
            await self._conn.commit()
            changed = cursor.rowcount
        return await self.get_booking(booking_id) if changed else None

    async def reschedule_booking(
        self, booking_id: int, starts_at: datetime, service: Service
    ) -> Booking:
        """Перенести запись на другое время с той же проверкой занятости."""
        ends_at = starts_at + service.duration
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                async with self._conn.execute(
                    "SELECT master_id FROM bookings WHERE id = ? AND status = 'active'",
                    (booking_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    raise SlotTaken("запись уже неактивна")

                master_id = row["master_id"]
                conflict_filter = (
                    "" if master_id is None else " AND (master_id = ? OR master_id IS NULL)"
                )
                # Саму переносимую запись из проверки исключаем: она вполне
                # может пересекаться со своим же новым временем.
                params: list = [_iso(ends_at), _iso(starts_at), booking_id]
                if master_id is not None:
                    params.append(master_id)
                async with self._conn.execute(
                    "SELECT 1 FROM bookings WHERE status = 'active'"
                    " AND starts_at < ? AND ends_at > ? AND id <> ?" + conflict_filter,
                    params,
                ) as cursor:
                    if await cursor.fetchone():
                        raise SlotTaken(starts_at.isoformat())

                await self._conn.execute(
                    "UPDATE bookings SET starts_at = ?, ends_at = ?, reminded_at = NULL"
                    " WHERE id = ?",
                    (_iso(starts_at), _iso(ends_at), booking_id),
                )
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise

        booking = await self.get_booking(booking_id)
        assert booking is not None
        return booking

    # ── напоминания ───────────────────────────────────────────────────────

    async def due_reminders(self, now: datetime, lead: timedelta) -> list[Booking]:
        """Активные записи, до которых осталось меньше `lead` и о которых ещё не напоминали.

        Записи, что уже начались, сюда не попадают: напоминание «через час»,
        пришедшее после начала, только раздражает.
        """
        async with self._lock, self._conn.execute(
            _BOOKING_SELECT + " WHERE b.status = 'active' AND b.reminded_at IS NULL"
            " AND b.starts_at > ? AND b.starts_at <= ? ORDER BY b.starts_at",
            (_iso(now), _iso(now + lead)),
        ) as cursor:
            return [_booking(row) for row in await cursor.fetchall()]

    async def mark_reminded(self, booking_id: int) -> None:
        async with self._lock:
            await self._conn.execute(
                "UPDATE bookings SET reminded_at = ? WHERE id = ?",
                (_iso(datetime.now(UTC)), booking_id),
            )
            await self._conn.commit()

    async def close_past(self, now: datetime) -> int:
        """Перевести завершившиеся записи в `done`, чтобы они не висели активными."""
        async with self._lock:
            cursor = await self._conn.execute(
                "UPDATE bookings SET status = ? WHERE status = 'active' AND ends_at <= ?",
                (STATUS_DONE, _iso(now)),
            )
            await self._conn.commit()
            return cursor.rowcount

    # ── статистика ────────────────────────────────────────────────────────

    async def stats(self, days: int = 30) -> dict[str, int]:
        since = _iso(datetime.now(UTC) - timedelta(days=days))
        async with self._lock, self._conn.execute(
            "SELECT status, COUNT(*) AS total FROM bookings WHERE created_at >= ?"
            " GROUP BY status",
            (since,),
        ) as cursor:
            rows = await cursor.fetchall()
        counts = {row["status"]: int(row["total"]) for row in rows}
        return {
            "active": counts.get(STATUS_ACTIVE, 0),
            "done": counts.get(STATUS_DONE, 0),
            "cancelled": counts.get(STATUS_CANCELLED, 0),
            "total": sum(counts.values()),
        }

    async def top_services(self, days: int = 30, limit: int = 5) -> list[tuple[str, int]]:
        since = _iso(datetime.now(UTC) - timedelta(days=days))
        async with self._lock, self._conn.execute(
            "SELECT s.title AS title, COUNT(*) AS total FROM bookings b"
            " JOIN services s ON s.id = b.service_id"
            " WHERE b.created_at >= ? AND b.status <> 'cancelled'"
            " GROUP BY s.id ORDER BY total DESC LIMIT ?",
            (since, limit),
        ) as cursor:
            return [(row["title"], int(row["total"])) for row in await cursor.fetchall()]

    # ── прочее ────────────────────────────────────────────────────────────

    async def get_setting(self, key: str) -> str | None:
        async with self._lock, self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        async with self._lock:
            await self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await self._conn.commit()


# ──────────────────────────────────────────────────────────────────────────────
# Преобразование строк
# ──────────────────────────────────────────────────────────────────────────────

_BOOKING_SELECT = """
SELECT b.*, s.title AS service_title, s.duration_min AS duration_min,
       COALESCE(m.name, '') AS master_name,
       COALESCE(c.name, '') AS client_name, COALESCE(c.phone, '') AS client_phone
FROM bookings b
JOIN services s ON s.id = b.service_id
LEFT JOIN masters m ON m.id = b.master_id
LEFT JOIN clients c ON c.tg_id = b.client_id
"""


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("в базу пишем только aware datetime")
    return moment.astimezone(UTC).isoformat()


def _dt(raw: str) -> datetime:
    return datetime.fromisoformat(raw).astimezone(UTC)


def _service(row: aiosqlite.Row) -> Service:
    return Service(
        id=int(row["id"]),
        title=row["title"],
        duration_min=int(row["duration_min"]),
        price=row["price"],
        description=row["description"],
        active=bool(row["active"]),
        position=int(row["position"]),
    )


def _master(row: aiosqlite.Row) -> Master:
    return Master(id=int(row["id"]), name=row["name"], active=bool(row["active"]))


def _window(row: aiosqlite.Row) -> WorkWindow:
    return WorkWindow(
        weekday=int(row["weekday"]),
        start_min=int(row["start_min"]),
        end_min=int(row["end_min"]),
        master_id=row["master_id"],
    )


def _booking(row: aiosqlite.Row) -> Booking:
    return Booking(
        id=int(row["id"]),
        client_id=int(row["client_id"]),
        service_id=int(row["service_id"]),
        starts_at=_dt(row["starts_at"]),
        ends_at=_dt(row["ends_at"]),
        master_id=row["master_id"],
        status=row["status"],
        comment=row["comment"],
        created_at=_dt(row["created_at"]),
        reminded_at=_dt(row["reminded_at"]) if row["reminded_at"] else None,
        service_title=row["service_title"],
        master_name=row["master_name"],
        client_name=row["client_name"],
        client_phone=row["client_phone"],
    )
