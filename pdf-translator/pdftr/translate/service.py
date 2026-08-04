"""Перевод пачки абзацев: кэш, дедупликация, потоки, прогресс.

Между разбором PDF и переводчиком нужен слой, который знает про объёмы. На
книге в триста страниц абзацев десятки тысяч, и отправлять их по одному —
несколько часов. Здесь одинаковые тексты схлопываются в один запрос, известные
берутся из кэша, а остальные уходят пачками в несколько потоков.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from .cache import Cache
from .google import GoogleTranslator, TranslationError, split_batches, split_long

_LETTERS = re.compile(r"[^\W\d_]", re.UNICODE)
# Строка из одних цифр, знаков и пробелов: «12.3», «(с) 2024», «— 4 —».
_NOTHING_TO_TRANSLATE = re.compile(r"^[\W\d_]*$", re.UNICODE)


def worth_translating(text: str) -> bool:
    """Есть ли в строке что переводить."""
    stripped = text.strip()
    if len(stripped) < 2:
        return False
    if _NOTHING_TO_TRANSLATE.match(stripped):
        return False
    return bool(_LETTERS.search(stripped))


class Service:
    """Переводчик уровня документа."""

    def __init__(
        self,
        cache: Cache,
        engine: GoogleTranslator | None = None,
        *,
        workers: int = 4,
    ) -> None:
        self.cache = cache
        self.engine = engine or GoogleTranslator()
        self.workers = max(1, workers)
        self.detected = ""
        self._lock = threading.Lock()

    def translate_all(
        self,
        texts: list[str],
        source: str,
        target: str,
        progress: Callable[[int, int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> list[str]:
        """Перевести список текстов, сохранив порядок.

        `progress` зовут по мере готовности пачек, `should_stop` — способ
        отменить работу из другого потока: задачу могли снять из интерфейса.
        """
        result = list(texts)
        wanted = sorted({text for text in texts if worth_translating(text)})
        if not wanted:
            return result

        # Длинные абзацы режем по предложениям сразу: и в запрос они иначе не
        # влезут, и кэш должен хранить те же куски, что будут спрошены потом.
        origin: dict[str, list[str]] = {text: split_long(text) for text in wanted}
        pieces = list(dict.fromkeys(part for parts in origin.values() for part in parts))

        known = self.cache.get_many(pieces, source, target)
        missing = [piece for piece in pieces if piece not in known]
        total = len(missing)
        if progress:
            progress(0, total)

        collected = dict(known)
        if missing:
            collected.update(
                self._fetch(missing, source, target, progress, should_stop, total)
            )

        table = {
            text: " ".join(collected.get(part, part) for part in parts).strip()
            for text, parts in origin.items()
        }
        for index, text in enumerate(texts):
            hit = table.get(text)
            if hit:
                result[index] = hit
        return result

    def _fetch(
        self,
        missing: list[str],
        source: str,
        target: str,
        progress: Callable[[int, int], None] | None,
        should_stop: Callable[[], bool] | None,
        total: int,
    ) -> dict[str, str]:
        unique = list(missing)
        batches = split_batches(unique)
        collected: dict[str, str] = {}
        done = 0
        failure: list[BaseException] = []

        def handle(batch: list[int]) -> None:
            nonlocal done
            if failure or (should_stop and should_stop()):
                return
            chunk = [unique[index] for index in batch]
            try:
                translated, detected = self.engine.translate_batch(chunk, source, target)
            except TranslationError as error:
                failure.append(error)
                return
            with self._lock:
                if detected and not self.detected:
                    self.detected = detected
                for original, value in zip(chunk, translated, strict=False):
                    collected[original] = value or original
                done += len(chunk)
                if progress:
                    progress(min(done, total), total)

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            list(pool.map(handle, batches))
        if failure:
            raise failure[0]

        # Кэш общий для всех задач: следующий такой же документ переведётся сразу.
        self.cache.put_many(collected, source, target)
        return collected
