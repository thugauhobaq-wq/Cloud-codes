"""Общие приспособления для тестов.

Сети в тестах нет: движки подменяются заглушками с тем же интерфейсом. Фото
тоже не настоящие — достаточно правильных первых байтов, потому что сервер
смотрит только на них, а движок-заглушка не смотрит вовсе.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hwocr.recognize import RecognitionError

# --- картинки --------------------------------------------------------------

JPEG = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 8 + b"\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 32
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 32


@pytest.fixture()
def jpeg_bytes() -> bytes:
    return JPEG


@pytest.fixture()
def png_bytes() -> bytes:
    return PNG


@pytest.fixture()
def webp_bytes() -> bytes:
    return WEBP


@pytest.fixture()
def heic_bytes() -> bytes:
    return HEIC


# --- движок-заглушка -------------------------------------------------------


class FakeRecognizer:
    """Отвечает заданным текстом или падает заданной ошибкой."""

    name = "fake"

    def __init__(
        self,
        text: str = "Привет, мир",
        *,
        fail: str | None = None,
        crash: bool = False,
        delay: float = 0.0,
    ) -> None:
        self.text = text
        self.fail = fail
        self.crash = crash
        self.delay = delay
        self.calls: list[tuple[int, str]] = []

    def recognize(self, image: bytes, media_type: str) -> str:
        self.calls.append((len(image), media_type))
        if self.delay:
            time.sleep(self.delay)
        if self.crash:
            raise ZeroDivisionError("неожиданно")
        if self.fail:
            raise RecognitionError(self.fail)
        return self.text


# --- заглушка клиента Anthropic ---------------------------------------------


class Block:
    def __init__(self, type: str, text: str = "") -> None:
        self.type = type
        self.text = text
        self.thinking = text


class StopDetails:
    def __init__(self, explanation: str = "") -> None:
        self.explanation = explanation


class Response:
    def __init__(self, content, stop_reason: str = "end_turn", stop_details=None) -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.stop_details = stop_details


class _Messages:
    def __init__(self, owner: FakeClaudeClient) -> None:
        self.owner = owner

    def create(self, **kwargs):
        self.owner.calls.append(kwargs)
        outcome = self.owner.outcome
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _Beta:
    def __init__(self, owner: FakeClaudeClient) -> None:
        self.messages = _Messages(owner)


class FakeClaudeClient:
    """`client.messages.create` и `client.beta.messages.create` в одном флаконе."""

    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.calls: list[dict] = []
        self.messages = _Messages(self)
        self.beta = _Beta(self)


def text_response(*texts: str, stop_reason: str = "end_turn") -> Response:
    return Response([Block("text", text) for text in texts], stop_reason)


class ApiError(Exception):
    """Похожа на anthropic.APIStatusError: есть status_code и message."""

    def __init__(self, status_code: int, message: str = "нет") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class APIConnectionError(Exception):
    """Имя класса совпадает с настоящим — адаптер узнаёт его по имени."""


# --- заглушка сети для Яндекса ----------------------------------------------


class Recorder:
    """Запоминает запросы, отдаёт заранее заданные ответы по очереди."""

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def http_error(code: int, body: bytes = b"") -> Exception:
    import io
    import urllib.error

    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(body))


# --- ожидание --------------------------------------------------------------


def wait_for(check, timeout: float = 10.0):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("не дождались")
