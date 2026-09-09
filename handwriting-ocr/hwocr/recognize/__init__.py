"""Выбор движка по настройкам окружения.

Ключи живут только в переменных окружения (в `.env` рядом с compose): в
командной строке они попали бы в историю и список процессов, в базе — в бэкап.
Проверка «ключ есть» делается здесь, один раз при старте, и падает громко:
лучше сервер не поднимется, чем каждая фотография будет заканчиваться ошибкой.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from .base import MEDIA_TYPES, RecognitionError, Recognizer, sniff_image

ENGINES = ("claude", "yandex")
DEFAULT_ENGINE = "claude"


class ConfigError(RecognitionError):
    """В окружении не хватает настроек для выбранного движка."""


def build_recognizer(env: Mapping[str, str] | None = None) -> Recognizer:
    env = os.environ if env is None else env
    engine = (env.get("OCR_ENGINE") or DEFAULT_ENGINE).strip().lower()

    if engine == "claude":
        key = (env.get("ANTHROPIC_API_KEY") or "").strip()
        if not key:
            raise ConfigError(
                "OCR_ENGINE=claude, но ANTHROPIC_API_KEY не задан — "
                "впишите ключ в .env (см. .env.example)"
            )
        from .claude import DEFAULT_MODEL, ClaudeRecognizer

        return ClaudeRecognizer(
            api_key=key,
            model=(env.get("CLAUDE_MODEL") or DEFAULT_MODEL).strip(),
            fallbacks=(env.get("CLAUDE_FALLBACKS") or "1").strip() not in ("0", "false", "no"),
        )

    if engine == "yandex":
        key = (env.get("YC_API_KEY") or "").strip()
        folder = (env.get("YC_FOLDER_ID") or "").strip()
        pairs = (("YC_API_KEY", key), ("YC_FOLDER_ID", folder))
        missing = [name for name, value in pairs if not value]
        if missing:
            raise ConfigError(
                f"OCR_ENGINE=yandex, но не задан {' и '.join(missing)} — "
                "впишите в .env (см. .env.example)"
            )
        from .yandex import YandexRecognizer

        return YandexRecognizer(key, folder)

    raise ConfigError(
        f"неизвестный движок OCR_ENGINE={engine!r}; допустимо: {', '.join(ENGINES)}"
    )


__all__ = [
    "DEFAULT_ENGINE",
    "ENGINES",
    "MEDIA_TYPES",
    "ConfigError",
    "RecognitionError",
    "Recognizer",
    "build_recognizer",
    "sniff_image",
]
