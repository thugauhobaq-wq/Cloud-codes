"""Общие приспособления для тестов.

Главное здесь — `FakeDownloader`. Сети в тестах нет ни в одном: настоящий
`yt-dlp` ходит на чужие площадки, а те отвечают по-разному в зависимости от дня
недели и страны, из которой пришёл запрос. Проверять надо не их, а наш код
вокруг: очередь, лимиты, отдачу файла. Поэтому загрузчик подменяется заглушкой,
которая умеет всё то же самое — рапортовать прогресс, падать, зависать и
отдавать файл, — но делает это мгновенно и предсказуемо.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vsaver.downloader import AUDIO_FORMAT, Cancelled, DownloadError, Meta, Progress, Result
from vsaver.jobs import Queue, Settings
from vsaver.limits import Limits
from vsaver.store import AUDIO, Store


@dataclass
class FakeDownloader:
    """Загрузчик, который ничего не качает.

    Пишет заданное число байт, дёргая прогресс столько раз, сколько попросят,
    и умеет изображать любую беду: отказ на опросе, отказ на скачивании и
    долгую закачку, которую можно отменить снаружи.
    """

    title: str = "Тестовый ролик"
    duration: int = 120
    size: int = 4096
    ext: str = "mp4"
    steps: int = 4
    probe_error: str = ""
    fetch_error: str = ""
    block: threading.Event | None = None
    probes: list[str] = field(default_factory=list)
    fetches: list[str] = field(default_factory=list)
    started: threading.Event = field(default_factory=threading.Event)

    def probe(self, url: str, *, kind: str = "video", quality: str = "1080") -> Meta:
        self.probes.append(url)
        if self.probe_error:
            raise DownloadError(self.probe_error)
        return Meta(
            title=self.title,
            duration=self.duration,
            size=self.size,
            ext=AUDIO_FORMAT if kind == AUDIO else self.ext,
            site="test",
        )

    def fetch(
        self,
        url: str,
        target: Path,
        *,
        kind: str = "video",
        quality: str = "1080",
        report,
        should_stop,
    ) -> Result:
        self.fetches.append(url)
        self.started.set()
        if self.fetch_error:
            raise DownloadError(self.fetch_error)

        ext = AUDIO_FORMAT if kind == AUDIO else self.ext
        path = Path(f"{target}.{ext}")
        chunk = max(1, self.size // max(1, self.steps))
        written = 0
        with path.open("wb") as handle:
            for _ in range(self.steps):
                if should_stop():
                    path.unlink(missing_ok=True)
                    raise Cancelled
                if self.block is not None:
                    # Даём тесту время отменить задачу ровно посередине.
                    self.block.wait(timeout=5)
                piece = min(chunk, self.size - written)
                handle.write(b"\0" * piece)
                written += piece
                report(Progress(downloaded=written, total=self.size, speed=chunk, eta=1))
        if should_stop():
            path.unlink(missing_ok=True)
            raise Cancelled
        report(Progress(downloaded=written, total=self.size, stage="done"))
        return Result(
            path=path,
            title=self.title,
            ext=ext,
            size=path.stat().st_size,
            duration=self.duration,
        )


@pytest.fixture
def store(tmp_path):
    made = Store(tmp_path / "data")
    yield made
    made.close()


@pytest.fixture
def downloader():
    return FakeDownloader()


@pytest.fixture
def limits():
    """Потолки для тестов: мелкие, чтобы упираться в них быстро и намеренно."""
    return Limits(
        per_hour=3,
        per_ip_active=2,
        queue_length=5,
        max_size=1024 * 1024,
        max_duration=600,
        disk_quota=8 * 1024 * 1024,
        keep_hours=1.0,
        keep_count=10,
    )


@pytest.fixture
def running(store, downloader, limits):
    """Запущенная очередь с заглушкой вместо загрузчика."""
    queue = Queue(store, Settings(workers=1, limits=limits), downloader)
    queue.start()
    yield queue
    queue.stop(timeout=2)


def wait_for(check, timeout: float = 5.0) -> bool:
    """Дождаться условия, не завися от скорости машины."""
    import time

    edge = time.monotonic() + timeout
    while time.monotonic() < edge:
        if check():
            return True
        time.sleep(0.02)
    return check()


def wait_status(store: Store, job_id: str, *statuses: str, timeout: float = 5.0):
    """Дождаться, пока задача придёт в одно из состояний, и вернуть её."""
    wanted = set(statuses)
    wait_for(
        lambda: (store.get(job_id) is not None and store.get(job_id).status in wanted),
        timeout,
    )
    return store.get(job_id)
