"""Каркас хранилища на SQLite.

Наследник описывает свою схему строкой и пользуется готовым: открытие,
блокировка на общее соединение, транзакции, key-value настройки и разбор
времени. Всё то, что в каждом боте писалось заново — и в каждом чуть иначе.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

log = logging.getLogger(__name__)

#: Таблица настроек есть у всех: пауза рассылки, версия схемы, время
#: последнего опроса — что-нибудь такое заводится в каждом боте.
_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def to_iso(moment: datetime) -> str:
    """Момент в UTC-строку для базы. Naive datetime не принимаем осознанно.

    Смешение aware и naive в одной колонке — источник ошибок, которые
    вылезают через месяц при переходе на летнее время.
    """
    if moment.tzinfo is None:
        raise ValueError("в базу пишем только aware datetime")
    return moment.astimezone(UTC).isoformat()


def from_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    return datetime.fromisoformat(raw).astimezone(UTC)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def rulower(value: str | None) -> str:
    """Нижний регистр с кириллицей.

    Встроенный `lower()` в SQLite умеет только латиницу: «Чехол» так и
    остаётся «Чехол», и поиск по-русски не находит ничего. Регистрируется в
    соединении как SQL-функция `rulower`.
    """
    return value.lower() if isinstance(value, str) else ""


class BaseStorage:
    """Общая часть хранилища.

        class Storage(BaseStorage):
            schema = "CREATE TABLE IF NOT EXISTS orders (...);"

            async def add_order(self, title: str) -> int:
                return await self.execute("INSERT INTO orders (title) VALUES (?)", (title,))
    """

    #: SQL наследника; выполняется одним `executescript` при открытии.
    schema: str = ""

    def __init__(self, db_path: str) -> None:
        self._path = Path(db_path)
        self._db: aiosqlite.Connection | None = None
        # Хендлеры бота и фоновые воркеры ходят в базу из одного цикла
        # событий; без блокировки на общем соединении получаются гонки.
        self._lock = asyncio.Lock()

    # ── жизненный цикл ────────────────────────────────────────────────────

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_BASE_SCHEMA + self.schema)
        # WAL — чтобы чтение отчётов не блокировалось записью из хендлеров.
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.create_function("rulower", 1, rulower, deterministic=True)
        await self._db.commit()
        await self.on_open()

    async def on_open(self) -> None:
        """Точка расширения: миграции, предзаполнение, прогрев кеша."""

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> BaseStorage:
        await self.open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError(f"{type(self).__name__}.open() не вызван")
        return self._db

    # ── запросы ───────────────────────────────────────────────────────────

    async def fetch_all(self, sql: str, params: Sequence = ()) -> list[aiosqlite.Row]:
        async with self._lock, self.connection.execute(sql, params) as cursor:
            return list(await cursor.fetchall())

    async def fetch_one(self, sql: str, params: Sequence = ()) -> aiosqlite.Row | None:
        async with self._lock, self.connection.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def fetch_value(self, sql: str, params: Sequence = (), default=None):
        row = await self.fetch_one(sql, params)
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    async def execute(self, sql: str, params: Sequence = ()) -> int:
        """Выполнить запрос с коммитом. Возвращает `lastrowid` для INSERT."""
        async with self._lock:
            cursor = await self.connection.execute(sql, params)
            await self.connection.commit()
            return int(cursor.lastrowid or 0)

    async def execute_changes(self, sql: str, params: Sequence = ()) -> int:
        """То же, но возвращает число затронутых строк — для UPDATE и DELETE.

        Именно им отличается «отменил запись» от «отменять было нечего»:
        кнопку под сообщением нажимают дважды чаще, чем кажется.
        """
        async with self._lock:
            cursor = await self.connection.execute(sql, params)
            await self.connection.commit()
            return cursor.rowcount

    async def executemany(self, sql: str, rows: Iterable[Sequence]) -> None:
        batch = list(rows)
        if not batch:
            return
        async with self._lock:
            await self.connection.executemany(sql, batch)
            await self.connection.commit()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Проверить и записать одним куском.

            async with self.transaction() as conn:
                async with conn.execute("SELECT ... ") as cursor:
                    if await cursor.fetchone():
                        raise SlotTaken
                await conn.execute("INSERT ...")

        Внутри работаем с выданным соединением напрямую: методы хранилища
        берут ту же блокировку и здесь встанут намертво.

        Нужно это там, где между проверкой и записью нельзя никого пустить:
        два клиента на один слот, последняя единица товара на две корзины.
        """
        async with self._lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
                await self.connection.commit()
            except BaseException:
                await self.connection.rollback()
                raise

    # ── настройки ─────────────────────────────────────────────────────────

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = await self.fetch_one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    async def get_flag(self, key: str, default: bool = False) -> bool:
        raw = await self.get_setting(key)
        return default if raw is None else raw == "1"

    async def set_flag(self, key: str, value: bool) -> None:
        await self.set_setting(key, "1" if value else "0")
