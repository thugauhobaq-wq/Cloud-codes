"""Хранилище задач: SQLite рядом с файлами.

Состояние живёт на диске, а не в памяти процесса, по простой причине: перевод
книги идёт минутами, за это время сервер успевают и перезапустить, и обновить.
После перезапуска задачи не теряются — незаконченные встают обратно в очередь,
а уже переведённое достаётся из кэша переводов почти мгновенно.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS job (
    id        TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    source    TEXT NOT NULL,
    target    TEXT NOT NULL,
    status    TEXT NOT NULL,
    stage     TEXT NOT NULL DEFAULT '',
    percent   INTEGER NOT NULL DEFAULT 0,
    pages     INTEGER NOT NULL DEFAULT 0,
    pages_done INTEGER NOT NULL DEFAULT 0,
    size      INTEGER NOT NULL DEFAULT 0,
    out_size  INTEGER NOT NULL DEFAULT 0,
    detected  TEXT NOT NULL DEFAULT '',
    error     TEXT NOT NULL DEFAULT '',
    warnings  TEXT NOT NULL DEFAULT '[]',
    created   REAL NOT NULL,
    started   REAL,
    finished  REAL
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
    """Одна задача перевода, как её видит интерфейс."""

    id: str
    name: str
    source: str
    target: str
    status: str
    stage: str = ""
    percent: int = 0
    pages: int = 0
    pages_done: int = 0
    size: int = 0
    out_size: int = 0
    detected: str = ""
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    created: float = 0.0
    started: float | None = None
    finished: float | None = None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "source": self.source,
            "target": self.target,
            "status": self.status,
            "stage": self.stage,
            "percent": self.percent,
            "pages": self.pages,
            "pagesDone": self.pages_done,
            "size": self.size,
            "outSize": self.out_size,
            "detected": self.detected,
            "error": self.error,
            "warnings": self.warnings,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
        }


def new_id() -> str:
    return uuid.uuid4().hex[:16]


class Store:
    """Записи о задачах и пути к их файлам."""

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

    def source_path(self, job_id: str) -> Path:
        return self.files / f"{job_id}.src.pdf"

    def result_path(self, job_id: str) -> Path:
        return self.files / f"{job_id}.out.pdf"

    # --- записи ------------------------------------------------------------

    def create(self, name: str, source: str, target: str, size: int) -> Job:
        job = Job(
            id=new_id(),
            name=name,
            source=source,
            target=target,
            status=QUEUED,
            size=size,
            created=time.time(),
        )
        with self._lock:
            self._db.execute(
                "INSERT INTO job (id, name, source, target, status, size, created)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job.id, job.name, job.source, job.target, job.status, job.size, job.created),
            )
            self._db.commit()
        return job

    def update(self, job_id: str, **fields: object) -> None:
        if not fields:
            return
        if "warnings" in fields:
            fields["warnings"] = json.dumps(fields["warnings"], ensure_ascii=False)
        columns = ", ".join(f"{key} = ?" for key in fields)
        with self._lock:
            self._db.execute(
                f"UPDATE job SET {columns} WHERE id = ?", (*fields.values(), job_id)
            )
            self._db.commit()

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
        for path in (self.source_path(job_id), self.result_path(job_id)):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
        return removed

    def sweep(self, keep_hours: float, keep_count: int) -> int:
        """Убрать старьё: телефон присылает файлы, диск не резиновый."""
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
    try:
        warnings = json.loads(row["warnings"] or "[]")
    except (ValueError, TypeError):
        warnings = []
    return Job(
        id=row["id"],
        name=row["name"],
        source=row["source"],
        target=row["target"],
        status=row["status"],
        stage=row["stage"],
        percent=row["percent"],
        pages=row["pages"],
        pages_done=row["pages_done"],
        size=row["size"],
        out_size=row["out_size"],
        detected=row["detected"],
        error=row["error"],
        warnings=warnings if isinstance(warnings, list) else [],
        created=row["created"],
        started=row["started"],
        finished=row["finished"],
    )
