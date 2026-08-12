"""Хранилище задач: SQLite рядом со скачанными файлами.

Состояние живёт на диске, а не в памяти процесса: закачка идёт минутами, за это
время сервер успевают и перезапустить, и обновить. После перезапуска задачи не
теряются — незаконченные встают обратно в очередь.

Сервис открытый, поэтому у каждой задачи есть два дополнительных поля, которых
не бывает в личных инструментах: `owner` — случайный номер сессии, по нему
человеку показывают его собственные задачи и ничьи больше, и `ip` — по нему
считается частота запросов. Оба живут ровно столько же, сколько сама задача,
и уезжают вместе с ней при первой же уборке.
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
    url        TEXT NOT NULL,
    site       TEXT NOT NULL DEFAULT '',
    title      TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL DEFAULT 'video',
    quality    TEXT NOT NULL DEFAULT '',
    ext        TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL,
    stage      TEXT NOT NULL DEFAULT '',
    percent    INTEGER NOT NULL DEFAULT 0,
    total      INTEGER NOT NULL DEFAULT 0,
    downloaded INTEGER NOT NULL DEFAULT 0,
    speed      INTEGER NOT NULL DEFAULT 0,
    eta        INTEGER NOT NULL DEFAULT 0,
    duration   INTEGER NOT NULL DEFAULT 0,
    owner      TEXT NOT NULL DEFAULT '',
    ip         TEXT NOT NULL DEFAULT '',
    error      TEXT NOT NULL DEFAULT '',
    created    REAL NOT NULL,
    started    REAL,
    finished   REAL
);
CREATE INDEX IF NOT EXISTS job_created ON job(created DESC);
CREATE INDEX IF NOT EXISTS job_owner ON job(owner, created DESC);
"""

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
ACTIVE = (QUEUED, RUNNING)

VIDEO = "video"
AUDIO = "audio"


@dataclass(slots=True)
class Job:
    """Одна задача скачивания, как её видит интерфейс."""

    id: str
    url: str
    site: str = ""
    title: str = ""
    kind: str = VIDEO
    quality: str = ""
    ext: str = ""
    status: str = QUEUED
    stage: str = ""
    percent: int = 0
    total: int = 0
    downloaded: int = 0
    speed: int = 0
    eta: int = 0
    duration: int = 0
    owner: str = ""
    ip: str = ""
    error: str = ""
    created: float = 0.0
    started: float | None = None
    finished: float | None = None

    def as_dict(self) -> dict:
        """То, что уходит в браузер. Ни `owner`, ни `ip` наружу не отдаются."""
        return {
            "id": self.id,
            "url": self.url,
            "site": self.site,
            "title": self.title,
            "kind": self.kind,
            "quality": self.quality,
            "ext": self.ext,
            "status": self.status,
            "stage": self.stage,
            "percent": self.percent,
            "total": self.total,
            "downloaded": self.downloaded,
            "speed": self.speed,
            "eta": self.eta,
            "duration": self.duration,
            "error": self.error,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
        }

    @property
    def filename(self) -> str:
        """Имя, под которым файл сохранится у человека."""
        stem = (self.title or "видео").strip() or "видео"
        return f"{stem}.{self.ext}" if self.ext else stem


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def new_owner() -> str:
    return uuid.uuid4().hex


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

    def file_path(self, job_id: str, ext: str = "") -> Path:
        """Куда лёг (или ляжет) готовый файл.

        Расширение приходит от загрузчика и в имя файла на диске попадает только
        после чистки: имя задаём мы, но подсказку о нём — чужая сторона.
        """
        suffix = "".join(ch for ch in ext.lower() if ch.isalnum())[:8]
        return self.files / (f"{job_id}.{suffix}" if suffix else job_id)

    def find_file(self, job_id: str) -> Path | None:
        """Найти файл задачи, не зная расширения: пригодится при уборке."""
        for path in self.files.glob(f"{job_id}*"):
            if path.is_file():
                return path
        return None

    # --- записи ------------------------------------------------------------

    def create(
        self,
        url: str,
        *,
        site: str = "",
        kind: str = VIDEO,
        quality: str = "",
        owner: str = "",
        ip: str = "",
    ) -> Job:
        job = Job(
            id=new_id(),
            url=url,
            site=site,
            kind=kind,
            quality=quality,
            status=QUEUED,
            owner=owner,
            ip=ip,
            created=time.time(),
        )
        with self._lock:
            self._db.execute(
                "INSERT INTO job (id, url, site, kind, quality, status, owner, ip, created)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job.id,
                    job.url,
                    job.site,
                    job.kind,
                    job.quality,
                    job.status,
                    job.owner,
                    job.ip,
                    job.created,
                ),
            )
            self._db.commit()
        return job

    def update(self, job_id: str, **fields: object) -> None:
        if not fields:
            return
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

    def recent(self, limit: int = 50, *, owner: str = "") -> list[Job]:
        """Последние задачи. С `owner` — только свои."""
        with self._lock:
            if owner:
                rows = self._db.execute(
                    "SELECT * FROM job WHERE owner = ? ORDER BY created DESC LIMIT ?",
                    (owner, limit),
                ).fetchall()
            else:
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

    def count_active(self, *, owner: str = "", ip: str = "") -> int:
        """Сколько задач в работе — всего, у одной сессии или с одного адреса."""
        query = "SELECT COUNT(*) FROM job WHERE status IN (?, ?)"
        params: list[object] = [QUEUED, RUNNING]
        if owner:
            query += " AND owner = ?"
            params.append(owner)
        if ip:
            query += " AND ip = ?"
            params.append(ip)
        with self._lock:
            return int(self._db.execute(query, params).fetchone()[0])

    def count_since(self, ip: str, since: float) -> int:
        """Сколько задач этот адрес создал начиная с момента `since`."""
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM job WHERE ip = ? AND created >= ?", (ip, since)
            ).fetchone()
        return int(row[0])

    def delete(self, job_id: str) -> bool:
        with self._lock:
            cursor = self._db.execute("DELETE FROM job WHERE id = ?", (job_id,))
            self._db.commit()
            removed = cursor.rowcount > 0
        for path in self.files.glob(f"{job_id}*"):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
        return removed

    def disk_used(self) -> int:
        """Сколько места занято скачанным. Считается по каталогу, а не по базе:
        недокачанные куски `yt-dlp` в базе не значатся, а место занимают."""
        total = 0
        for path in self.files.iterdir():
            with contextlib.suppress(OSError):
                if path.is_file():
                    total += path.stat().st_size
        return total

    def sweep(self, keep_hours: float, keep_count: int) -> int:
        """Убрать старьё: на открытом сервисе это главный способ не утонуть."""
        edge = time.time() - keep_hours * 3600
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM job WHERE status NOT IN (?, ?)"
                " AND COALESCE(finished, created) < ?"
                " AND id NOT IN (SELECT id FROM job ORDER BY created DESC LIMIT ?)",
                (QUEUED, RUNNING, edge, keep_count),
            ).fetchall()
        removed = 0
        for row in rows:
            if self.delete(row["id"]):
                removed += 1
        return removed

    def sweep_oldest(self, count: int = 1) -> int:
        """Снести самые старые готовые задачи, не глядя на срок.

        Нужно, когда диск подошёл к квоте: место надо освободить сейчас, а не
        через положенные два часа.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM job WHERE status NOT IN (?, ?)"
                " ORDER BY COALESCE(finished, created) ASC LIMIT ?",
                (QUEUED, RUNNING, max(0, count)),
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
        url=row["url"],
        site=row["site"],
        title=row["title"],
        kind=row["kind"],
        quality=row["quality"],
        ext=row["ext"],
        status=row["status"],
        stage=row["stage"],
        percent=row["percent"],
        total=row["total"],
        downloaded=row["downloaded"],
        speed=row["speed"],
        eta=row["eta"],
        duration=row["duration"],
        owner=row["owner"],
        ip=row["ip"],
        error=row["error"],
        created=row["created"],
        started=row["started"],
        finished=row["finished"],
    )


__all__ = [
    "ACTIVE",
    "AUDIO",
    "CANCELLED",
    "DONE",
    "FAILED",
    "QUEUED",
    "RUNNING",
    "VIDEO",
    "Job",
    "Store",
    "new_id",
    "new_owner",
]
