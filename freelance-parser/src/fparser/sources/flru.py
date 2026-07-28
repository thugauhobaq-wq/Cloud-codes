"""FL.ru — общая лента проектов."""

from __future__ import annotations

import re

from .base import FeedItem, extract_budget
from .rss import RssSource

# FL.ru дописывает бюджет прямо в заголовок: «Вёрстка лендинга (5000 руб.)»
# или «Бот на Python (от 15000 до 30 000 руб.)». В сообщение бюджет попадёт
# отдельной строкой, поэтому из заголовка убираем оба варианта — и одиночную
# сумму, и вилку.
_BUDGET_TAIL_RE = re.compile(
    r"\s*[\(\[]\s*(?:бюджет\s*:?\s*)?(?:от\s*)?[\d\s.]+"
    r"(?:\s*(?:-|—|–|до)\s*[\d\s.]+)?\s*(?:руб\.?|рублей|₽|р\.)[^)\]]*[\)\]]\s*$",
    re.IGNORECASE,
)


class FlRuSource(RssSource):
    slug = "flru"
    title = "FL.ru"
    feed_url = "https://www.fl.ru/rss/all.xml"
    id_pattern = r"/projects/(\d+)"

    def clean_title(self, title: str) -> str:
        return _BUDGET_TAIL_RE.sub("", title).strip()

    def extract_budget(self, item: FeedItem) -> tuple[int | None, int | None]:
        # Заголовок разбираем первым: там бюджет записан однозначно, тогда как в
        # описании легко наткнуться на сумму из текста задания («бюджет на рекламу
        # 50 000 руб.»), которая к оплате исполнителю отношения не имеет.
        low, high = extract_budget(item.title)
        if low is not None or high is not None:
            return low, high
        return extract_budget(item.description)
