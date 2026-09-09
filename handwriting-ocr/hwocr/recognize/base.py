"""Общий контракт движков распознавания.

Движков два — Claude и Yandex Vision, — и у них разные API, лимиты и ошибки.
Очередь и сервер про это знать не должны: им нужен объект с методом
`recognize(байты, тип) -> текст` и одно исключение с понятным человеку текстом.
Всё, что за пределами этого контракта, остаётся внутри адаптера.
"""

from __future__ import annotations

from typing import Protocol

# Что принимаем от телефона. HEIC с iPhone сюда намеренно не входит: ни один
# из движков его не читает, а приложение само переводит такие фото в JPEG.
MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


class RecognitionError(RuntimeError):
    """Движок не ответил, отказал или ответил не тем.

    Текст исключения показывается прямо в карточке на телефоне, поэтому он
    пишется по-русски и объясняет, что делать, а не что сломалось внутри.
    """


class Recognizer(Protocol):
    """Один вызов — одна фотография — один текст."""

    name: str

    def recognize(self, image: bytes, media_type: str) -> str: ...


def sniff_image(head: bytes) -> str | None:
    """Тип картинки по первым байтам; None — это не картинка.

    Заголовку `Content-Type` от браузера верить нельзя: Android присылает
    `application/octet-stream`, а «Поделиться» — что угодно. HEIC отличается
    от «не картинки» отдельно, чтобы владелец iPhone получил внятный совет,
    а не безликое «формат не поддерживается».
    """
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"hevc", b"mif1", b"msf1"):
        return "image/heic"
    return None


__all__ = ["MEDIA_TYPES", "RecognitionError", "Recognizer", "sniff_image"]
