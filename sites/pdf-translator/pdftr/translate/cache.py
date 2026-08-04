"""Кэш переводов: один и тот же текст не переводим дважды.

В большом документе повторов больше, чем кажется: колонтитулы, подписи под
таблицами, названия разделов в оглавлении и в тексте, шапки на каждой странице.
На книге в четыреста страниц кэш снимает заметную долю обращений — а значит, и
времени, и риска упереться в ограничение частоты.

Кэш общий для всех задач: перевели инструкцию сегодня — завтра её же переведём
мгновенно.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS phrase (
    id      TEXT PRIMARY KEY,
    source  TEXT NOT NULL,
    target  TEXT NOT NULL,
    text    TEXT NOT NULL,
    created REAL NOT NULL DEFAULT (julianday('now'))
);
CREATE INDEX IF NOT EXISTS phrase_created ON phrase(created);
"""


def key_of(text: str, source: str, target: str) -> str:
    digest = hashlib.sha256(f"{source}\x00{target}\x00{text}".encode())
    return digest.hexdigest()[:32]


class Cache:
    """Хранилище переводов в SQLite. Потокобезопасно: одно соединение под замком."""

    def __init__(self, path: Path | str | None) -> None:
        self.path = str(path) if path else ":memory:"
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        # WAL нужен, чтобы чтение кэша не спорило с записью из другого потока.
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.commit()

    def get_many(self, texts: list[str], source: str, target: str) -> dict[str, str]:
        """Найти сразу все известные переводы. Ключ ответа — исходный текст."""
        if not texts:
            return {}
        wanted = {key_of(text, source, target): text for text in texts}
        found: dict[str, str] = {}
        keys = list(wanted)
        with self._lock:
            for start in range(0, len(keys), 400):
                chunk = keys[start : start + 400]
                marks = ",".join("?" * len(chunk))
                rows = self._db.execute(
                    f"SELECT id, text FROM phrase WHERE id IN ({marks})", chunk
                ).fetchall()
                for row_id, translated in rows:
                    found[wanted[row_id]] = translated
        return found

    def put_many(self, pairs: dict[str, str], source: str, target: str) -> None:
        if not pairs:
            return
        rows = [
            (key_of(text, source, target), source, target, translated)
            for text, translated in pairs.items()
        ]
        with self._lock:
            self._db.executemany(
                "INSERT OR REPLACE INTO phrase (id, source, target, text) VALUES (?, ?, ?, ?)",
                rows,
            )
            self._db.commit()

    def size(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT count(*) FROM phrase").fetchone()[0])

    def trim(self, keep: int = 200_000) -> int:
        """Оставить только свежие записи — кэш не должен расти бесконечно."""
        with self._lock:
            total = int(self._db.execute("SELECT count(*) FROM phrase").fetchone()[0])
            if total <= keep:
                return 0
            self._db.execute(
                "DELETE FROM phrase WHERE id IN ("
                "  SELECT id FROM phrase ORDER BY created ASC LIMIT ?)",
                (total - keep,),
            )
            self._db.commit()
            return total - keep

    def close(self) -> None:
        with self._lock:
            self._db.close()
