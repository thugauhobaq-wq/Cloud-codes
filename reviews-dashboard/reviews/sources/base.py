"""Базовый интерфейс источника отзывов."""

from __future__ import annotations

from typing import Any

from ..models import Review


class Source:
    """Все парсеры реализуют один и тот же контракт.

    ``collect`` возвращает список ``Review``; поля ``sentiment``/``score``
    проставляются позже, в конвейере (``pipeline.collect``).
    """

    name: str = "base"
    title: str = "Источник"
    #: Что писать в --query: ссылка, id организации, артикул и т.п.
    query_hint: str = ""
    #: Нужны ли ключи/куки, работает ли без настройки.
    needs_key: bool = False

    def collect(self, query: str, limit: int = 100, **options: Any) -> list[Review]:
        raise NotImplementedError

    def parse_payload(self, payload: str, company: str) -> list[Review]:
        """Разбор заранее сохранённого ответа (HTML/JSON) — для офлайн-режима."""
        raise NotImplementedError(f"Источник {self.name} не поддерживает --from-file")

    # ------------------------------------------------------------- утилиты

    @staticmethod
    def company_key(query: str, explicit: str | None = None) -> str:
        """Человекочитаемый ключ компании: либо явный, либо из запроса."""
        if explicit:
            return explicit.strip()
        query = (query or "").strip()
        if query.startswith("http"):
            return query.rstrip("/").split("/")[-1] or query
        return query

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "query_hint": self.query_hint,
            "needs_key": self.needs_key,
        }
