"""Форматирование для русскоязычного интерфейса."""

from __future__ import annotations

from html import escape as _html_escape


def escape(text: str | None) -> str:
    """Экранировать пользовательские данные для parse_mode=HTML.

    Имя вида «<b>Вася</b>» без этого ломает разметку всего сообщения.
    """
    return _html_escape(text or "", quote=False)


def plural(count: int, one: str, few: str, many: str) -> str:
    """«1 заявка», «2 заявки», «5 заявок»."""
    remainder_100 = abs(count) % 100
    remainder_10 = abs(count) % 10
    if 11 <= remainder_100 <= 14:
        return many
    if remainder_10 == 1:
        return one
    if 2 <= remainder_10 <= 4:
        return few
    return many


def counted(count: int, one: str, few: str, many: str) -> str:
    return f"{count} {plural(count, one, few, many)}"


def money(amount: int, currency: str = "₽") -> str:
    """1500 → «1 500 ₽». Тысячи разделяем узким пробелом обычной ширины."""
    return f"{amount:,}".replace(",", " ") + (f" {currency}" if currency else "")


def human_duration(minutes: int) -> str:
    hours, rest = divmod(int(minutes), 60)
    if hours and rest:
        return f"{hours} ч {rest} мин"
    if hours:
        return f"{hours} ч"
    return f"{rest} мин"


def short(text: str, limit: int = 60) -> str:
    """Обрезать до длины, по которой подпись кнопки ещё читается."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def normalize_phone(raw: str) -> str:
    """Привести телефон к единому виду; непонятное оставить как есть.

    Клиент присылает «89001234567», «+7 (900) 123-45-67» и «9001234567» —
    в базе и в уведомлении владельцу это должно выглядеть одинаково.
    """
    digits = "".join(char for char in (raw or "") if char.isdigit())
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    elif len(digits) != 10:
        return (raw or "").strip()
    return f"+7 {digits[0:3]} {digits[3:6]}-{digits[6:8]}-{digits[8:]}"


def looks_like_phone(raw: str) -> bool:
    digits = sum(char.isdigit() for char in raw or "")
    return 7 <= digits <= 15 and all(char.isdigit() or char in "+()- " for char in raw or "")
