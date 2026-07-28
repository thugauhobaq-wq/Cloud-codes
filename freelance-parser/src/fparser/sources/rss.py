"""Базовый класс для площадок, отдающих RSS/Atom.

Три из четырёх бирж публикуют ленту, и разбор у них отличается только мелочами —
шаблоном идентификатора в ссылке да мусором в заголовке. Общая часть здесь.
"""

from __future__ import annotations

import logging

from ..models import Order
from .base import FeedItem, Fetcher, Source, extract_budget, id_from_url, parse_feed

log = logging.getLogger(__name__)


class RssSource(Source):
    """Площадка с RSS-лентой."""

    #: Регулярка с одной группой — идентификатор заказа в ссылке.
    #: Если не задана, берётся последнее длинное число в URL.
    id_pattern: str | None = None

    async def fetch(self, fetcher: Fetcher) -> list[Order]:
        response = await fetcher.get(self.feed_url)
        items = parse_feed(response.content)

        orders: list[Order] = []
        for item in items:
            order = self.to_order(item)
            if order is not None:
                orders.append(order)

        log.debug("%s: из ленты разобрано %d заказов", self.slug, len(orders))
        return orders

    def to_order(self, item: FeedItem) -> Order | None:
        title = self.clean_title(item.title)
        if not title:
            return None

        budget_min, budget_max = self.extract_budget(item)

        return Order(
            source=self.slug,
            native_id=self.extract_id(item),
            title=title,
            url=item.link,
            description=item.description,
            budget_min=budget_min,
            budget_max=budget_max,
            category=item.category,
            published_at=item.published_at,
        )

    def extract_id(self, item: FeedItem) -> str:
        """Идентификатор заказа.

        Ссылка надёжнее guid: guid у части площадок содержит метку времени и
        меняется между опросами, что превратило бы дедупликацию в фикцию.
        """
        return id_from_url(item.link, self.id_pattern)

    def extract_budget(self, item: FeedItem) -> tuple[int | None, int | None]:
        return extract_budget(f"{item.title}\n{item.description}")

    def clean_title(self, title: str) -> str:
        return title.strip()
