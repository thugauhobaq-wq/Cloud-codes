"""Хранилище задач: SQLite рядом с фотографиями.

Состояние лежит на диске, а не в памяти процесса: распознавание занимает
секунды, но сервер могут перезапустить в любой момент, а телефон — закрыть
приложение, не дождавшись. После рестарта незаконченные задачи встают обратно
в очередь, а готовый текст никуда не девается.

Результат хранится прямо в базе, а не файлом: это текст на пару килобайт, и
править его (пользователь исправляет ошибки распознавания) удобнее одной
командой UPDATE. Фотография же лежит файлом и удаляется вместе с задачей —
на ней могут быть чужие имена и адреса, хранить такое дольше нужного незачем.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS job (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    status     TEXT NOT NULL,
    engine     TEXT NOT NULL DEFAULT '',
    media_type TEXT NOT NULL DEFAULT '',
    size       INTEGER NOT NULL DEFAULT 0,
    text       TEXT NOT NULL DEFAULT '',
    edited     INTEGER NOT NULL DEFAULT 0,
    error      TEXT NOT NULL DEFAULT '',
    created    REAL NOT NULL,
    started    REAL,
    finished   REAL
);
CREATE INDEX IF NOT EXISTS job_created ON job(created DESC);
"""

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
ACTIVE = (QUEUED, RUNNING)


@dataclass(slots=True)
class Job:
    """Одна фотография, как её видит интерфейс."""

    id: str
    name: str
    status: str
    engine: str = ""
    media_type: str = ""
    size: int = 0
    text: str = ""
    edited: bool = False
    error: str = ""
    created: float = 0.0
    started: float | None = None
    finished: float | None = None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "engine": self.engine,
            "mediaType": self.media_type,
            "size": self.size,
            "text": self.text,
            "edited": self.edited,
            "error": self.error,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
        }


def new_id() -> str:
    return uuid.uuid4().hex[:16]


class Store:
    """Записи о задачах и пути к их фотографиям."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.files = self.root / "files"
        self.files.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.root / "jobs.sqlite", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.commit()

    # --- пути --------------------------------------------------------------

    def image_path(self, job_id: str) -> Path:
        # Расширение одно на все форматы: настоящий тип лежит в записи,
        # а сервер отдаёт его в Content-Type. Так не нужна карта расширений.
        return self.files / f"{job_id}.img"

    # --- записи ------------------------------------------------------------

    def create(self, name: str, media_type: str, size: int, engine: str = "") -> Job:
        job = Job(
            id=new_id(),
            name=name,
            status=QUEUED,
            engine=engine,
            media_type=media_type,
            size=size,
            created=time.time(),
        )
        with self._lock:
            self._db.execute(
                "INSERT INTO job (id, name, status, engine, media_type, size, created)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job.id, job.name, job.status, job.engine, job.media_type, job.size, job.created),
            )
            self._db.commit()
        return job

    def update(self, job_id: str, **fields: object) -> None:
        if not fields:
            return
        if "edited" in fields:
            fields["edited"] = 1 if fields["edited"] else 0
        columns = ", ".join(f"{key} = ?" for key in fields)
        with self._lock:
            self._db.execute(
                f"UPDATE job SET {columns} WHERE id = ?", (*fields.values(), job_id)
            )
            self._db.commit()

    def set_text(self, job_id: str, text: str, *, edited: bool) -> None:
        """Записать текст: сразу после распознавания или после правки человеком."""
        self.update(job_id, text=text, edited=edited)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
        return _job_of(row) if row else None

    def recent(self, limit: int = 50) -> list[Job]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM job ORDER BY created DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_job_of(row) for row in rows]

    def pending(self) -> list[Job]:
        """Задачи, которые надо (до)делать: очередь плюс прерванные рестартом."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM job WHERE status IN (?, ?) ORDER BY created ASC",
                (QUEUED, RUNNING),
            ).fetchall()
        return [_job_of(row) for row in rows]

    def delete(self, job_id: str) -> bool:
        with self._lock:
            cursor = self._db.execute("DELETE FROM job WHERE id = ?", (job_id,))
            self._db.commit()
            removed = cursor.rowcount > 0
        with contextlib.suppress(OSError):
            self.image_path(job_id).unlink(missing_ok=True)
        return removed

    def sweep(self, keep_hours: float, keep_count: int) -> int:
        """Убрать старьё целиком — и запись, и фото.

        Правило одно и понятное владельцу: через `keep_hours` результат с
        сервера пропадает, нужное надо скопировать себе. Держать текст без фото
        было бы можно, но тогда пришлось бы объяснять два срока вместо одного.
        """
        edge = time.time() - keep_hours * 3600
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM job WHERE status NOT IN (?, ?)"
                " AND finished < ?"
                " AND id NOT IN (SELECT id FROM job ORDER BY created DESC LIMIT ?)",
                (QUEUED, RUNNING, edge, keep_count),
            ).fetchall()
        removed = 0
        for row in rows:
            if self.delete(row["id"]):
                removed += 1
        return removed

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _job_of(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        name=row["name"],
        status=row["status"],
        engine=row["engine"],
        media_type=row["media_type"],
        size=row["size"],
        text=row["text"],
        edited=bool(row["edited"]),
        error=row["error"],
        created=row["created"],
        started=row["started"],
        finished=row["finished"],
    )
