"""Распознавание через Yandex Vision OCR, модель `handwritten`.

Вариант для тех, кому важно, чтобы фото не покидало Россию: сервис работает в
российских дата-центрах, оплата в рублях. По качеству на сложной прописи он
уступает языковой модели, но печатные и аккуратные рукописные тексты читает
уверенно.

Всё на urllib: SDK Яндекса тянет за собой gRPC и десяток пакетов, а нужен
один POST с JSON. В тестах вместо сети подставляется `opener`, принимающий
готовый `Request`, — так проверяются и адрес, и заголовки, и тело.

Формат запроса сверен с исходниками API (`yandex-cloud/cloudapi`, ocr v1) и
примером из документации; вживую из среды разработки адаптер не проверялся —
адрес сервиса закрыт прокси. Проверка — на настоящем сервере, см. README.
"""

from __future__ import annotations

import base64
import json
import random
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from .base import RecognitionError

ENDPOINT = "https://ocr.api.cloud.yandex.net/ocr/v1/recognizeText"
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # предел сервиса на одну картинку
# Сервис ждёт не MIME-тип, а короткое имя формата, и WebP не принимает вовсе.
# Телефон и так шлёт JPEG; WebP может прийти только через «Поделиться».
FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG"}
# Модель `handwritten` знает только русский и английский.
DEFAULT_LANGUAGES = ("ru",)

Opener = Callable[[urllib.request.Request], bytes]


class YandexRecognizer:
    """Один POST с повторами при временных отказах."""

    name = "yandex"

    def __init__(
        self,
        api_key: str,
        folder_id: str,
        *,
        languages: tuple[str, ...] = DEFAULT_LANGUAGES,
        attempts: int = 3,
        timeout: float = 60.0,
        opener: Opener | None = None,
    ) -> None:
        self.api_key = api_key
        self.folder_id = folder_id
        self.languages = list(languages) or list(DEFAULT_LANGUAGES)
        self.attempts = max(1, attempts)
        self.timeout = timeout
        self._open = opener or self._request

    # --- сеть -------------------------------------------------------------

    def _request(self, request: urllib.request.Request) -> bytes:
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read()

    def _build(self, image: bytes, media_type: str) -> urllib.request.Request:
        payload = json.dumps(
            {
                "mimeType": FORMATS[media_type],
                "languageCodes": self.languages,
                "model": "handwritten",
                "content": base64.standard_b64encode(image).decode("ascii"),
            }
        ).encode("utf-8")
        return urllib.request.Request(
            ENDPOINT,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Api-Key {self.api_key}",
                "x-folder-id": self.folder_id,
                "Content-Type": "application/json",
                # Просим сервис не сохранять фото у себя для дообучения.
                "x-data-logging-enabled": "false",
            },
        )

    # --- публичное ---------------------------------------------------------

    def recognize(self, image: bytes, media_type: str) -> str:
        if media_type not in FORMATS:
            raise RecognitionError(
                "Яндекс принимает только JPEG и PNG — откройте фото в приложении, "
                "оно само переведёт его в JPEG"
            )
        if len(image) > MAX_IMAGE_BYTES:
            limit = MAX_IMAGE_BYTES // (1024 * 1024)
            raise RecognitionError(f"фото больше {limit} МБ — Яндекс такое не принимает")
        request = self._build(image, media_type)

        last = ""
        for attempt in range(self.attempts):
            try:
                raw = self._open(request)
            except urllib.error.HTTPError as error:
                body = _read_error(error)
                if error.code in (401, 403):
                    raise RecognitionError(
                        "ключ Яндекса не подошёл — проверьте YC_API_KEY и YC_FOLDER_ID в .env"
                    ) from error
                if error.code == 400:
                    raise RecognitionError(f"Яндекс не принял запрос: {body}") from error
                if error.code not in (429, 500, 502, 503, 504):
                    raise RecognitionError(f"Яндекс отказал: HTTP {error.code}") from error
                last = f"HTTP {error.code}"
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last = str(getattr(error, "reason", None) or error)
            else:
                try:
                    return self._parse(raw)
                except ValueError as error:
                    raise RecognitionError(f"непонятный ответ Яндекса: {error}") from error
            if attempt + 1 < self.attempts:
                # Пауза удваивается плюс случайная добавка, как и в других
                # сетевых клиентах репозитория.
                time.sleep(2**attempt + random.random())
        raise RecognitionError(f"Яндекс не ответил ({last})")

    @staticmethod
    def _parse(raw: bytes) -> str:
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as error:
            raise ValueError("это не JSON") from error
        try:
            text = data["result"]["textAnnotation"]["fullText"]
        except (KeyError, TypeError) as error:
            raise ValueError("нет поля result.textAnnotation.fullText") from error
        if not isinstance(text, str):
            raise ValueError("fullText — не строка")
        text = text.strip()
        if not text:
            raise RecognitionError("на фото не нашлось рукописного текста")
        return text


def _read_error(error: urllib.error.HTTPError) -> str:
    try:
        body = error.read().decode("utf-8", "replace")
    except Exception:
        return ""
    try:
        return str(json.loads(body).get("message") or body)[:200]
    except (ValueError, AttributeError):
        return body[:200]


__all__ = ["ENDPOINT", "YandexRecognizer"]
