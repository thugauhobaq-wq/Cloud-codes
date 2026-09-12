"""hh.ru — официальный API поиска вакансий.

Токен не нужен: личный кабинет на dev.hh.ru требуется, только если приложение
запрашивает авторизацию пользователя, а поиск вакансий открыт.

Это вакансии, а не разовые заказы, — поток по характеру другой. Для поиска
клиентов на ботов он всё равно полезен: «удалённо» плюс «проектная работа»
регулярно дают ровно такие задачи.
"""

from __future__ import annotations

import logging
from typing import Any

from ..models import Order
from .base import Fetcher, Source, parse_date, strip_html

log = logging.getLogger(__name__)

API_URL = "https://api.hh.ru/vacancies"

#: hh просит приложения представляться осмысленно, а не браузерным User-Agent.
#: Почту сюда не кладём: адрес владельца незачем рассылать по чужим сервисам.
USER_AGENT = "freelance-parser/0.1 (+https://github.com/thugauhobaq-wq/Cloud-codes)"

DEFAULT_QUERY = "telegram бот"

#: Коды валют hh к знакам. Чего нет в списке, показываем кодом как есть.
_CURRENCY_SIGNS = {"RUR": "₽", "RUB": "₽", "USD": "$", "EUR": "€"}


class HhSource(Source):
    slug = "hh"
    title = "hh.ru"
    feed_url = API_URL

    def __init__(self, feed_url: str | None = None, query: str | None = None) -> None:
        super().__init__(feed_url)
        self.query = (query or DEFAULT_QUERY).strip() or DEFAULT_QUERY

    async def fetch(self, fetcher: Fetcher) -> list[Order]:
        response = await fetcher.get(
            self.feed_url,
            params={
                "text": self.query,
                "schedule": "remote",
                "order_by": "publication_time",
                "per_page": 50,
                # Сутки: опрашиваем часто, а более старое всё равно отсеет
                # дедупликация — незачем таскать лишние страницы.
                "period": 1,
            },
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        return self.parse(response.json())

    def parse(self, payload: dict[str, Any]) -> list[Order]:
        items = payload.get("items")
        if not isinstance(items, list):
            return []

        orders: list[Order] = []
        for item in items:
            order = self._to_order(item)
            if order is not None:
                orders.append(order)
        return orders

    def _to_order(self, item: dict[str, Any]) -> Order | None:
        if not isinstance(item, dict):
            return None

        native_id = item.get("id")
        title = item.get("name")
        if not native_id or not title:
            return None

        budget_min, budget_max, currency = _salary(item.get("salary"))

        return Order(
            source=self.slug,
            native_id=str(native_id),
            title=strip_html(str(title)),
            url=str(item.get("alternate_url") or f"https://hh.ru/vacancy/{native_id}"),
            description=_description(item),
            budget_min=budget_min,
            budget_max=budget_max,
            currency=currency,
            category=_category(item),
            published_at=_published(item),
        )


def _salary(salary: Any) -> tuple[int | None, int | None, str]:
    if not isinstance(salary, dict):
        return None, None, "₽"

    low = salary.get("from")
    high = salary.get("to")
    code = str(salary.get("currency") or "RUR").upper()

    return (
        int(low) if isinstance(low, int | float) else None,
        int(high) if isinstance(high, int | float) else None,
        _CURRENCY_SIGNS.get(code, code),
    )


def _description(item: dict[str, Any]) -> str:
    """Склеить требования и обязанности.

    hh отдаёт их отдельными кусками с подсветкой найденных слов в `<highlighttext>`;
    теги убираем, иначе они уедут в сообщение как есть.
    """
    snippet = item.get("snippet")
    if not isinstance(snippet, dict):
        return ""

    parts = [snippet.get("responsibility"), snippet.get("requirement")]
    return strip_html("\n".join(str(part) for part in parts if part))


def _category(item: dict[str, Any]) -> str | None:
    roles = item.get("professional_roles")
    if isinstance(roles, list) and roles and isinstance(roles[0], dict):
        name = roles[0].get("name")
        if name:
            return str(name)

    employer = item.get("employer")
    if isinstance(employer, dict) and employer.get("name"):
        return str(employer["name"])
    return None


def _published(item: dict[str, Any]):
    return parse_date(item.get("published_at"))
