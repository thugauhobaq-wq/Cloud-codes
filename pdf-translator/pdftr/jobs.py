"""Очередь переводов: фоновый работник, прогресс и отмена.

Перевод большого файла — минуты, а то и десятки минут. Держать на нём HTTP-запрос
нельзя: телефон уснёт, вкладка закроется, мобильная сеть моргнёт. Поэтому загрузка
и перевод разведены: запрос кладёт файл и возвращает номер задачи, а работа идёт в
фоне. Интерфейс подписывается на события и показывает прогресс, но может и просто
закрыться — задача от этого не пострадает.
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

from .pdf.embed import FontError
from .pdf.reader import EncryptedPDFError, PDFError
from .pipeline import Cancelled, Progress, translate_document
from .store import ACTIVE, CANCELLED, DONE, FAILED, QUEUED, RUNNING, Job, Store
from .translate.cache import Cache
from .translate.google import GoogleTranslator, TranslationError
from .translate.service import Service

Listener = Callable[[dict], None]


@dataclass(slots=True)
class Settings:
    """Настройки обработки, общие для всех задач."""

    workers: int = 4
    rate: float = 4.0
    font: str = ""
    keep_hours: float = 48.0
    keep_count: int = 40


class Queue:
    """Одна очередь, один работник, любое число подписчиков на прогресс.

    Работник один намеренно: параллельный перевод двух документов не ускоряет
    ничего — упирается всё в переводчик, а не в процессор, — зато удваивает
    расход памяти и частоту обращений.
    """

    def __init__(self, store: Store, settings: Settings | None = None) -> None:
        self.store = store
        self.settings = settings or Settings()
        self.cache = Cache(store.root / "cache.sqlite")
        self._queue: queue.Queue[str] = queue.Queue()
        self._cancelled: set[str] = set()
        self._listeners: dict[str, list[Listener]] = {}
        self._lock = threading.Lock()
        self._current: str | None = None
        self._worker: threading.Thread | None = None
        self._stopping = threading.Event()

    # --- запуск ------------------------------------------------------------

    def start(self) -> None:
        if self._worker is not None:
            return
        for job in self.store.pending():
            # Прерванные рестартом задачи начинаются заново — но перевод уже
            # сделанных страниц лежит в кэше, так что второй заход быстрый.
            if job.status == RUNNING:
                self.store.update(job.id, status=QUEUED, stage="", percent=0)
            self._queue.put(job.id)
        self._worker = threading.Thread(target=self._loop, name="pdftr-worker", daemon=True)
        self._worker.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stopping.set()
        self._queue.put("")
        if self._worker is not None:
            self._worker.join(timeout)
        self.cache.close()

    # --- задачи ------------------------------------------------------------

    def submit(self, name: str, source: str, target: str, path: Path) -> Job:
        """Поставить в очередь уже сохранённый файл."""
        size = path.stat().st_size if path.exists() else 0
        job = self.store.create(name, source, target, size)
        target_path = self.store.source_path(job.id)
        if path != target_path:
            path.replace(target_path)
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
                self.store.update(
                    job_id,
                    status=FAILED,
                    error="внутренняя ошибка: " + traceback.format_exc(limit=1).strip(),
                    finished=time.time(),
                )
                self._publish(job_id)
            finally:
                with self._lock:
                    self._current = None
                    self._cancelled.discard(job_id)

    def _run(self, job: Job) -> None:
        path = self.store.source_path(job.id)
        if not path.exists():
            self.store.update(
                job.id, status=FAILED, error="файл потерян", finished=time.time()
            )
            self._publish(job.id)
            return

        self.store.update(job.id, status=RUNNING, started=time.time(), stage="read", percent=0)
        self._publish(job.id)

        last_sent = 0.0

        def report(progress: Progress) -> None:
            nonlocal last_sent
            now = time.monotonic()
            # Раз в полсекунды: чаще — это лишние записи в базу и лишний трафик
            # на телефон, а глазу и так не уследить.
            if now - last_sent < 0.5 and progress.percent < 99:
                return
            last_sent = now
            self.store.update(
                job.id,
                stage=progress.stage,
                percent=progress.percent,
                pages=progress.pages_total,
                pages_done=progress.pages_done,
                detected=progress.detected,
            )
            self._publish(job.id)

        def should_stop() -> bool:
            with self._lock:
                return job.id in self._cancelled or self._stopping.is_set()

        engine = GoogleTranslator(rate=self.settings.rate)
        service = Service(self.cache, engine, workers=self.settings.workers)
        try:
            result = translate_document(
                path.read_bytes(),
                source=job.source,
                target=job.target,
                cache=self.cache,
                service=service,
                report=report,
                should_stop=should_stop,
                title=job.name,
            )
        except Cancelled:
            self.store.update(job.id, status=CANCELLED, finished=time.time())
            self._publish(job.id)
            return
        except EncryptedPDFError as error:
            self._fail(job.id, str(error))
            return
        except PDFError as error:
            self._fail(job.id, f"не удалось разобрать PDF: {error}")
            return
        except FontError as error:
            self._fail(job.id, str(error))
            return
        except TranslationError as error:
            self._fail(job.id, f"перевод не удался: {error}")
            return
        except MemoryError:
            self._fail(job.id, "файл слишком большой для этого сервера")
            return

        out = self.store.result_path(job.id)
        out.write_bytes(result.data)
        self.store.update(
            job.id,
            status=DONE,
            stage="done",
            percent=100,
            pages=result.pages,
            pages_done=result.pages,
            out_size=len(result.data),
            detected=result.detected or job.source,
            warnings=result.warnings,
            finished=time.time(),
        )
        self._publish(job.id)
        self.cache.trim()
        self.store.sweep(self.settings.keep_hours, self.settings.keep_count)

    def _fail(self, job_id: str, message: str) -> None:
        self.store.update(job_id, status=FAILED, error=message, finished=time.time())
        self._publish(job_id)
