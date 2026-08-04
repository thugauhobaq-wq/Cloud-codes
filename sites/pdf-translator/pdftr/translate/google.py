"""Клиент бесплатного переводчика Google.

Endpoint `translate_a/single` с `client=gtx` — тот же, которым пользуется
страница переводчика. Ключа он не требует, но и гарантий не даёт: может
ответить 429, может закрыться совсем. Поэтому здесь есть и повторы с
нарастающей паузой, и ограничитель частоты, и честная ошибка наверх, если
ответа так и не случилось.

Абзацы отправляются пачками через перевод строки: так одно обращение
переводит десяток абзацев вместо одного. Если ответ вернулся с другим числом
строк — пачка переспрашивается по одному, чтобы перевод не съехал на соседний
абзац.
"""

from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT = "https://translate.googleapis.com/translate_a/single"

# Опытный предел: около шести тысяч символов проходит, но у длинных запросов
# заметно растёт доля отказов. Четыре тысячи — спокойный режим.
BATCH_LIMIT = 4000
_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Mobile Safari/537.36"
)


class TranslationError(RuntimeError):
    """Переводчик не ответил или ответил не тем."""


class RateLimiter:
    """Не больше `rate` обращений в секунду на всех потоках сразу."""

    def __init__(self, rate: float) -> None:
        self.interval = 1.0 / rate if rate > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if self.interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next - now)
            self._next = max(now, self._next) + self.interval
        if delay:
            time.sleep(delay)


class GoogleTranslator:
    """Перевод строк с повторами, ограничением частоты и разбором ответа."""

    def __init__(
        self,
        *,
        rate: float = 4.0,
        attempts: int = 5,
        timeout: float = 30.0,
        opener: object | None = None,
    ) -> None:
        self.limiter = RateLimiter(rate)
        self.attempts = max(1, attempts)
        self.timeout = timeout
        # В тестах сюда подставляется заглушка вместо настоящей сети.
        self._open = opener or self._request

    # --- сеть -------------------------------------------------------------

    def _request(self, payload: bytes) -> bytes:
        request = urllib.request.Request(
            ENDPOINT,
            data=payload,
            headers={
                "User-Agent": _USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Accept": "*/*",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read()

    def _call(self, text: str, source: str, target: str) -> tuple[str, str]:
        """Одно обращение с повторами. Возвращает перевод и определённый язык."""
        payload = urllib.parse.urlencode(
            [
                ("client", "gtx"),
                ("sl", source or "auto"),
                ("tl", target),
                ("dt", "t"),
                ("ie", "UTF-8"),
                ("oe", "UTF-8"),
                ("q", text),
            ]
        ).encode("utf-8")

        last = ""
        for attempt in range(self.attempts):
            self.limiter.wait()
            try:
                raw = self._open(payload)
            except urllib.error.HTTPError as error:
                last = f"HTTP {error.code}"
                if error.code in (400, 404) and attempt == 0:
                    # Иногда 400 прилетает на совершенно нормальный текст —
                    # один повтор стоит попробовать, дальше смысла нет.
                    pass
                elif error.code not in (429, 500, 502, 503, 504):
                    raise TranslationError(
                        f"переводчик отказал: {last}"
                    ) from error
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last = str(error.reason if hasattr(error, "reason") else error)
            else:
                try:
                    return self._parse(raw)
                except (ValueError, TypeError, IndexError) as error:
                    last = f"непонятный ответ: {error}"
            if attempt + 1 < self.attempts:
                # Пауза удваивается, плюс случайная добавка — чтобы потоки не
                # ломились обратно одновременно.
                time.sleep(2 ** attempt + random.random())
        raise TranslationError(f"переводчик не ответил ({last})")

    @staticmethod
    def _parse(raw: bytes) -> tuple[str, str]:
        data = json.loads(raw.decode("utf-8", "replace"))
        chunks = data[0] if isinstance(data, list) and data else []
        if not isinstance(chunks, list):
            raise ValueError("нет блока с переводом")
        text = "".join(
            chunk[0] for chunk in chunks
            if isinstance(chunk, list) and chunk and isinstance(chunk[0], str)
        )
        detected = ""
        if isinstance(data, list) and len(data) > 2 and isinstance(data[2], str):
            detected = data[2]
        return text, detected

    # --- публичное ---------------------------------------------------------

    def translate(self, text: str, source: str, target: str) -> tuple[str, str]:
        """Перевести одну строку."""
        if not text.strip():
            return text, ""
        return self._call(text, source, target)

    def translate_batch(
        self, texts: list[str], source: str, target: str
    ) -> tuple[list[str], str]:
        """Перевести пачку строк одним обращением, если получится.

        Строки склеиваются переводом строки — Google сохраняет их разбивку.
        Если не сохранил, пачка переспрашивается по одной: лучше медленнее,
        чем перепутать местами абзацы.
        """
        if not texts:
            return [], ""
        prepared = [" ".join(item.split()) or " " for item in texts]
        if len(prepared) == 1:
            translated, detected = self._call(prepared[0], source, target)
            return [translated], detected

        joined = "\n".join(prepared)
        translated, detected = self._call(joined, source, target)
        parts = translated.split("\n")
        if len(parts) == len(prepared):
            return [part.strip() for part in parts], detected

        out: list[str] = []
        for item in prepared:
            single, single_detected = self._call(item, source, target)
            detected = detected or single_detected
            out.append(single.strip())
        return out, detected


def split_batches(texts: list[str], limit: int = BATCH_LIMIT) -> list[list[int]]:
    """Разложить строки по пачкам так, чтобы каждая влезла в один запрос.

    Возвращает списки номеров, а не сами строки: вызывающему нужно разложить
    переводы обратно по своим местам.
    """
    batches: list[list[int]] = []
    current: list[int] = []
    length = 0
    for index, text in enumerate(texts):
        size = len(text) + 1
        if size >= limit:
            if current:
                batches.append(current)
                current, length = [], 0
            batches.append([index])  # длинная строка едет одна
            continue
        if current and length + size > limit:
            batches.append(current)
            current, length = [], 0
        current.append(index)
        length += size
    if current:
        batches.append(current)
    return batches


def split_long(text: str, limit: int = BATCH_LIMIT) -> list[str]:
    """Разрезать слишком длинный абзац по границам предложений."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
        if cut < limit // 3:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        else:
            cut += 1
        parts.append(rest[:cut].strip())
        rest = rest[cut:]
    if rest.strip():
        parts.append(rest.strip())
    return parts
