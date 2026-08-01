"""Кодовые слова и deep-link.

Человек, которому в посте сказали «напишите боту слово ЧЕКЛИСТ», пришлёт что
угодно: «Чек-лист», «чеклист!», «/чеклист», «ЧЁКЛИСТ». Всё это должно
приводить к одному и тому же магниту, иначе воронка теряет людей на ровном
месте.
"""

from __future__ import annotations

import re

_PUNCTUATION_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES_RE = re.compile(r"\s+")

#: Длиннее этого кодовое слово не имеет смысла: его никто не перепечатает.
MAX_CODE_LENGTH = 32

#: Разделитель кода и метки источника в deep-link. Telegram разрешает в
#: payload только буквы, цифры, `_` и `-`, поэтому двойное подчёркивание.
SOURCE_SEPARATOR = "__"


def normalize(raw: str) -> str:
    """Привести пользовательский ввод к каноническому виду кодового слова."""
    text = (raw or "").strip().lower().replace("ё", "е")
    text = text.lstrip("/")
    text = _PUNCTUATION_RE.sub(" ", text)
    text = _SPACES_RE.sub("", text)
    return text[:MAX_CODE_LENGTH]


def looks_like_code(raw: str) -> bool:
    """Похоже ли сообщение на кодовое слово, а не на разговор.

    Отсекаем длинные фразы: по ним искать магнит бессмысленно, а отвечать
    «не знаю такого слова» на каждое «спасибо!» — грубо.
    """
    text = (raw or "").strip()
    if not text or len(text) > 64:
        return False
    return len(text.split()) <= 3


def parse_start_payload(raw: str) -> tuple[str, str]:
    """Разобрать payload из ссылки: `chek__instagram` → («chek», «instagram»)."""
    text = (raw or "").strip()
    if not text:
        return "", ""
    code, separator, source = text.partition(SOURCE_SEPARATOR)
    if not separator:
        return normalize(code), ""
    return normalize(code), normalize_source(source)


def normalize_source(raw: str) -> str:
    """Метка источника: оставляем узнаваемое, режем мусор."""
    text = (raw or "").strip().lower()
    text = re.sub(r"[^\w\-]+", "-", text, flags=re.UNICODE).strip("-")
    return text[:32]
