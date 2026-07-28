"""Отбор заказов по настройкам пользователя.

Чистые функции без обращений к сети и БД — поэтому логика отбора проверяется
тестами целиком, без запуска бота.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from .models import Order
from .storage import Filters


@dataclass(slots=True)
class MatchResult:
    passed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.passed


def normalize(text: str) -> str:
    """Привести текст к виду, пригодному для сравнения.

    «ё» приводим к «е»: на биржах пишут и так, и так, и из-за этого фильтр по
    слову «верстка» молча пропускал бы половину заказов на «вёрстку».
    """
    return text.lower().replace("ё", "е")


@lru_cache(maxsize=512)
def _word_pattern(word: str) -> re.Pattern[str]:
    """Шаблон «слово начинается с этой основы».

    Простое вхождение подстроки не годится: ключевое слово «бот» нашлось бы
    внутри «работа», и канал завалило бы мусором. Привязка к началу слова
    оставляет нужные формы («бота», «боты», «Telegram-бот»), но отсекает
    случайные совпадения в середине.
    """
    return re.compile(rf"(?<!\w){re.escape(normalize(word))}", re.IGNORECASE)


def contains_word(text: str, word: str) -> bool:
    word = word.strip()
    if not word:
        return False
    return _word_pattern(word).search(normalize(text)) is not None


def match(order: Order, filters: Filters) -> MatchResult:
    """Проверить заказ. Причина отказа возвращается для `dryrun` и логов."""
    if order.source not in filters.sources:
        return MatchResult(False, f"площадка {order.source} отключена")

    if filters.categories:
        category = normalize(order.category or "")
        allowed = {normalize(item) for item in filters.categories}
        if category not in allowed:
            return MatchResult(False, f"рубрика «{order.category or '—'}» не в списке")

    text = order.searchable_text
    for stopword in filters.stopwords:
        if contains_word(text, stopword):
            return MatchResult(False, f"стоп-слово «{stopword}»")

    if filters.keywords and not any(contains_word(text, word) for word in filters.keywords):
        return MatchResult(False, "нет ни одного ключевого слова")

    budget = order.budget_value
    if budget is None:
        if filters.min_budget > 0 and filters.skip_unknown_budget:
            return MatchResult(False, "бюджет не указан")
    elif budget < filters.min_budget:
        return MatchResult(False, f"бюджет {budget} меньше порога {filters.min_budget}")

    return MatchResult(True)


def apply(orders: list[Order], filters: Filters) -> list[Order]:
    return [order for order in orders if match(order, filters).passed]
