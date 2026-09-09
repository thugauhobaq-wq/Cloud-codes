"""Очередь распознавания: фоновый работник и подписчики.

Один вызов к движку — секунды, но держать на нём HTTP-запрос всё равно нельзя:
экран телефона гаснет, вкладка уходит в фон, мобильная сеть моргает. Поэтому
загрузка и распознавание разведены: запрос кладёт фото и возвращает номер
задачи, работа идёт в фоне, а интерфейс подписывается на её события.

Работник один намеренно: упирается всё в лимиты частоты у облачного API, а не
в процессор, и два потока лишь удвоят число отказов «слишком часто».
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .recognize import RecognitionError, Recognizer, build_recognizer
from .store import ACTIVE, CANCELLED, DONE, FAILED, QUEUED, RUNNING, Job, Store

Listener = Callable[[dict], None]


@dataclass(slots=True)
class Settings:
    """Сколько хранить результаты."""

    keep_hours: float = 24.0
    keep_count: int = 200


class Queue:
    """Одна очередь, один работник, любое число подписчиков."""

    def __init__(
        self,
        store: Store,
        settings: Settings | None = None,
        recognizer: Recognizer | None = None,
    ) -> None:
        self.store = store
        self.settings = settings or Settings()
        # Движок можно подставить снаружи (тесты, командная строка); иначе он
        # собирается из окружения при старте — и там же падает, если нет ключа.
        self._recognizer = recognizer
        self._queue: queue.Queue[str] = queue.Queue()
        self._cancelled: set[str] = set()
        self._listeners: dict[str, list[Listener]] = {}
        self._lock = threading.Lock()
        self._current: str | None = None
        self._worker: threading.Thread | None = None
        self._stopping = threading.Event()

    @property
    def recognizer(self) -> Recognizer:
        if self._recognizer is None:
            self._recognizer = build_recognizer()
        return self._recognizer

    # --- запуск ------------------------------------------------------------

    def start(self) -> None:
        if self._worker is not None:
            return
        _ = self.recognizer  # проверка настроек до того, как принимать фото
        for job in self.store.pending():
            # Прерванные рестартом задачи начинаются заново: ответ от движка
            # всё равно был потерян вместе с процессом.
            if job.status == RUNNING:
                self.store.update(job.id, status=QUEUED, started=None)
            self._queue.put(job.id)
        self._worker = threading.Thread(target=self._loop, name="hwocr-worker", daemon=True)
        self._worker.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stopping.set()
        self._queue.put("")
        if self._worker is not None:
            self._worker.join(timeout)

    # --- задачи ------------------------------------------------------------

    def submit(self, name: str, media_type: str, path: Path) -> Job:
        """Поставить в очередь уже сохранённое фото."""
        size = path.stat().st_size if path.exists() else 0
        job = self.store.create(name, media_type, size, engine=self.recognizer.name)
        target = self.store.image_path(job.id)
        if path != target:
            path.replace(target)
        self._queue.put(job.id)
        self._publish(job.id)
        return job

    def cancel(self, job_id: str) -> bool:
        job = self.store.get(job_id)
        if job is None or job.status not in ACTIVE:
            return False
        with self._lock:
            self._cancelled.add(job_id)
        if self._current != job_id:
            self.store.update(job_id, status=CANCELLED, finished=time.time())
            self._publish(job_id)
        return True

    def delete(self, job_id: str) -> bool:
        self.cancel(job_id)
        return self.store.delete(job_id)

    # --- подписка ----------------------------------------------------------

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
            # Отвалившийся слушатель — не наша забота: страницу могли закрыть.
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
                self._current = job_id
            try:
                self._run(job)
            except Exception:
                self._fail(
                    job_id,
                    "внутренняя ошибка: " + traceback.format_exc(limit=1).strip(),
                )
            finally:
                with self._lock:
                    self._current = None
                    self._cancelled.discard(job_id)

    def _run(self, job: Job) -> None:
        path = self.store.image_path(job.id)
        if not path.exists():
            self._fail(job.id, "фото потеряно — отправьте его ещё раз")
            return

        self.store.update(job.id, status=RUNNING, started=time.time())
        self._publish(job.id)

        try:
            text = self.recognizer.recognize(path.read_bytes(), job.media_type)
        except RecognitionError as error:
            self._fail(job.id, str(error))
            return

        with self._lock:
            cancelled = job.id in self._cancelled
        if cancelled:
            # Человек передумал, пока движок думал: результат выбрасываем.
            self.store.update(job.id, status=CANCELLED, finished=time.time())
            self._publish(job.id)
            return

        self.store.set_text(job.id, text, edited=False)
        self.store.update(job.id, status=DONE, finished=time.time())
        self._publish(job.id)
        self.store.sweep(self.settings.keep_hours, self.settings.keep_count)

    def _fail(self, job_id: str, message: str) -> None:
        self.store.update(job_id, status=FAILED, error=message, finished=time.time())
        self._publish(job_id)
