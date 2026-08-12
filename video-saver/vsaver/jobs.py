"""Очередь закачек: фоновые работники, прогресс и отмена.

Скачивание ролика — это от нескольких секунд до нескольких минут. Держать на нём
HTTP-запрос нельзя: вкладку закроют, телефон уснёт, мобильная сеть моргнёт.
Поэтому постановка задачи и сама работа разведены: запрос возвращает номер
задачи сразу, а качается всё в фоне. Страница подписывается на события и
показывает проценты, но может и закрыться — задача от этого не пострадает.

Работников несколько, в отличие от `pdf-translator`, где он один: там всё
упиралось в переводчика, а здесь — в сеть, и две параллельные закачки идут почти
вдвое быстрее одной. Сколько именно — настройка, потому что упереться можно уже
в канал самого сервера.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field

from .downloader import (
    DEFAULT_QUALITY,
    Cancelled,
    Downloader,
    DownloadError,
    Progress,
    YtDlp,
)
from .limits import Guard, LimitError, Limits
from .store import (
    ACTIVE,
    CANCELLED,
    DONE,
    FAILED,
    QUEUED,
    RUNNING,
    VIDEO,
    Job,
    Store,
)
from .urls import check as check_url

Listener = Callable[[dict], None]


@dataclass(slots=True)
class Settings:
    """Настройки обработки, общие для всех задач."""

    workers: int = 2
    limits: Limits = field(default_factory=Limits)


class Queue:
    """Одна очередь, несколько работников, любое число подписчиков на прогресс."""

    def __init__(
        self,
        store: Store,
        settings: Settings | None = None,
        downloader: Downloader | None = None,
    ) -> None:
        self.store = store
        self.settings = settings or Settings()
        self.guard = Guard(store, self.settings.limits)
        self.downloader = downloader or YtDlp(max_size=self.settings.limits.max_size)
        self._queue: queue.Queue[str] = queue.Queue()
        self._cancelled: set[str] = set()
        self._running: set[str] = set()
        self._listeners: dict[str, list[Listener]] = {}
        self._lock = threading.Lock()
        self._workers: list[threading.Thread] = []
        self._stopping = threading.Event()

    # --- запуск ------------------------------------------------------------

    def start(self) -> None:
        if self._workers:
            return
        for job in self.store.pending():
            # Прерванные рестартом задачи начинаются заново: докачивать нечего,
            # недокачанные куски всё равно снесены вместе с задачей.
            if job.status == RUNNING:
                self.store.update(job.id, status=QUEUED, stage="", percent=0, downloaded=0)
            self._queue.put(job.id)
        for number in range(max(1, self.settings.workers)):
            worker = threading.Thread(
                target=self._loop, name=f"vsaver-worker-{number + 1}", daemon=True
            )
            worker.start()
            self._workers.append(worker)

    def stop(self, timeout: float = 5.0) -> None:
        self._stopping.set()
        for _ in self._workers:
            self._queue.put("")
        for worker in self._workers:
            worker.join(timeout)
        self._workers.clear()

    # --- задачи ------------------------------------------------------------

    def submit(
        self,
        url: str,
        *,
        kind: str = VIDEO,
        quality: str = DEFAULT_QUALITY,
        owner: str = "",
        ip: str = "",
        resolve: bool = True,
    ) -> Job:
        """Проверить ссылку и лимиты, поставить задачу в очередь.

        Бросает `UrlError`, если ссылка не годится, и `LimitError`, если человек
        упёрся в потолок. И то и другое сервер показывает как есть.
        """
        target = check_url(url, resolve=resolve)
        self.guard.check_new_job(ip)
        job = self.store.create(
            target.url, site=target.site, kind=kind, quality=quality, owner=owner, ip=ip
        )
        self.guard.note_job(ip)
        self._queue.put(job.id)
        self._publish(job.id)
        return job

    def cancel(self, job_id: str) -> bool:
        job = self.store.get(job_id)
        if job is None or job.status not in ACTIVE:
            return False
        with self._lock:
            self._cancelled.add(job_id)
            running = job_id in self._running
        if not running:
            self.store.update(job_id, status=CANCELLED, finished=time.time())
            self._publish(job_id)
        return True

    def delete(self, job_id: str) -> bool:
        self.cancel(job_id)
        return self.store.delete(job_id)

    # --- подписка на прогресс ----------------------------------------------

    def listen(self, job_id: str, listener: Listener) -> Callable[[], None]:
        with self._lock:
            self._listeners.setdefault(job_id, []).append(listener)

        def cancel_listener() -> None:
            with self._lock:
                bucket = self._listeners.get(job_id) or []
                if listener in bucket:
                    bucket.remove(listener)
                if not bucket:
                    self._listeners.pop(job_id, None)

        return cancel_listener

    def _publish(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None:
            return
        with self._lock:
            listeners = list(self._listeners.get(job_id, ()))
        payload = job.as_dict()
        for listener in listeners:
            # Отвалившийся слушатель — не наша забота: вкладку могли закрыть.
            with contextlib.suppress(Exception):
                listener(payload)

    # --- работа ------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stopping.is_set():
            job_id = self._queue.get()
            if not job_id or self._stopping.is_set():
                continue
            job = self.store.get(job_id)
            if job is None or job.status not in ACTIVE:
                continue
            with self._lock:
                if job_id in self._cancelled:
                    self._cancelled.discard(job_id)
                    self.store.update(job_id, status=CANCELLED, finished=time.time())
                    self._publish(job_id)
                    continue
                self._running.add(job_id)
            try:
                self._run(job)
            except Exception:
                self.store.update(
                    job_id,
                    status=FAILED,
                    error="внутренняя ошибка: " + traceback.format_exc(limit=1).strip(),
                    finished=time.time(),
                )
                self._publish(job_id)
            finally:
                with self._lock:
                    self._running.discard(job_id)
                    self._cancelled.discard(job_id)

    def _run(self, job: Job) -> None:
        self.store.update(job.id, status=RUNNING, started=time.time(), stage="probe", percent=0)
        self._publish(job.id)

        def should_stop() -> bool:
            with self._lock:
                return job.id in self._cancelled or self._stopping.is_set()

        # 1. Спросить площадку, что это вообще такое.
        try:
            meta = self.downloader.probe(job.url, kind=job.kind, quality=job.quality)
        except DownloadError as error:
            return self._fail(job.id, str(error))
        if should_stop():
            return self._cancel(job.id)

        # 2. Отсеять заведомо неподъёмное — до того, как тратить трафик.
        try:
            self.guard.check_media(meta.duration, meta.size)
        except LimitError as error:
            return self._fail(job.id, str(error))

        self.store.update(
            job.id,
            title=meta.title,
            duration=meta.duration,
            total=meta.size,
            stage="download",
        )
        self._publish(job.id)

        # 3. Освободить место под ожидаемый файл.
        self.guard.free_space_for(meta.size or 0)

        last_sent = 0.0
        oversize = False

        def report(progress: Progress) -> None:
            nonlocal last_sent, oversize
            if self.guard.exceeds_size(progress.downloaded):
                # Обещанный размер бывает враньём, реальный — нет. Останавливаем
                # закачку тем же способом, что и отмену: через `should_stop`.
                oversize = True
                with self._lock:
                    self._cancelled.add(job.id)
                return
            now = time.monotonic()
            # Раз в полсекунды: чаще — лишние записи в базу и лишний трафик,
            # а глазу и так не уследить.
            if now - last_sent < 0.5 and progress.percent < 99 and progress.stage != "done":
                return
            last_sent = now
            self.store.update(
                job.id,
                stage=progress.stage,
                percent=progress.percent,
                downloaded=progress.downloaded,
                total=progress.total or meta.size,
                speed=progress.speed,
                eta=progress.eta,
            )
            self._publish(job.id)

        # 4. Собственно скачать.
        target = self.store.files / job.id
        try:
            result = self.downloader.fetch(
                job.url,
                target,
                kind=job.kind,
                quality=job.quality,
                report=report,
                should_stop=should_stop,
            )
        except Cancelled:
            if oversize:
                return self._fail(
                    job.id, "файл оказался больше допустимого — закачка остановлена"
                )
            return self._cancel(job.id)
        except DownloadError as error:
            return self._fail(job.id, str(error))
        except OSError as error:
            return self._fail(job.id, f"на сервере не осталось места: {error}")

        size = result.size or (result.path.stat().st_size if result.path.exists() else 0)
        if self.guard.exceeds_size(size):
            self.store.delete(job.id)
            return
        self.store.update(
            job.id,
            status=DONE,
            stage="done",
            percent=100,
            title=result.title or job.title,
            ext=result.ext,
            total=size,
            downloaded=size,
            speed=0,
            eta=0,
            duration=result.duration or meta.duration,
            finished=time.time(),
        )
        self._publish(job.id)
        self.store.sweep(self.settings.limits.keep_hours, self.settings.limits.keep_count)

    def _fail(self, job_id: str, message: str) -> None:
        self.store.update(job_id, status=FAILED, error=message, finished=time.time())
        self._publish(job_id)

    def _cancel(self, job_id: str) -> None:
        self.store.update(job_id, status=CANCELLED, finished=time.time())
        self._publish(job_id)


__all__ = ["Queue", "Settings"]
