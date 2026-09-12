"""Freelancehunt — официальный API v2.

Технически самый надёжный источник: документированный контракт вместо
скрейпинга, никакого антибота и никаких сюрпризов при редизайне сайта.
Взамен нужен токен — он берётся в разделе Apps and API и кладётся в `.env`.

Площадка украинская. Технических препятствий нет, но подходит ли аудитория и
устраивает ли вывод денег — решать владельцу; по умолчанию источник молчит,
пока не задан токен.
"""

from __future__ import annotations

import logging
from typing import Any

from ..models import Order
from .base import Fetcher, FetchError, Source, parse_date, strip_html

log = logging.getLogger(__name__)

API_URL = "https://api.freelancehunt.com/v2/projects"

_CURRENCY_SIGNS = {"UAH": "₴", "USD": "$", "EUR": "€", "RUB": "₽", "RUR": "₽"}


class FreelancehuntSource(Source):
    slug = "fh"
    title = "Freelancehunt"
    feed_url = API_URL

    def __init__(self, feed_url: str | None = None, token: str | None = None) -> None:
        super().__init__(feed_url)
        self.token = (token or "").strip()

    async def fetch(self, fetcher: Fetcher) -> list[Order]:
        if not self.token:
            # Внятная фраза вместо трейсбека: её видно и в `dryrun`, и в `/check`,
            # и из неё сразу понятно, что делать.
            raise FetchError(
                "не задан FREELANCEHUNT_TOKEN — получите ключ в разделе Apps and API "
                "и впишите его в .env, либо выключите площадку командой /sources"
            )

        if not self.token.isascii():
            # Заголовки HTTP допускают только ASCII. Без этой проверки опечатка
            # при копировании ключа падала бы неразборчивым UnicodeEncodeError
            # где-то в недрах httpx.
            raise FetchError(
                "FREELANCEHUNT_TOKEN содержит нелатинские символы — "
                "похоже, ключ скопировался с лишними знаками"
            )

        response = await fetcher.get(
            self.feed_url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
        )
        return self.parse(response.json())

    def parse(self, payload: dict[str, Any]) -> list[Order]:
        data = payload.get("data")
        if not isinstance(data, list):
            return []

        orders: list[Order] = []
        for item in data:
            order = self._to_order(item)
            if order is not None:
                orders.append(order)
        return orders

    def _to_order(self, item: Any) -> Order | None:
        if not isinstance(item, dict):
            return None

        native_id = item.get("id")
        attributes = item.get("attributes")
        if not native_id or not isinstance(attributes, dict):
            return None

        title = attributes.get("name")
        if not title:
            return None

        amount, currency = _budget(attributes.get("budget"))

        return Order(
            source=self.slug,
            native_id=str(native_id),
            title=strip_html(str(title)),
            url=_web_link(item, native_id),
            description=strip_html(str(attributes.get("description") or "")),
            # У проекта одна сумма, а не вилка: это предложенный заказчиком
            # бюджет, то есть нижняя граница разговора.
            budget_min=amount,
            currency=currency,
            category=_skill(attributes),
            published_at=parse_date(attributes.get("published_at")),
        )


def _budget(budget: Any) -> tuple[int | None, str]:
    if not isinstance(budget, dict):
        return None, "₴"

    amount = budget.get("amount")
    code = str(budget.get("currency") or "UAH").upper()
    value = int(amount) if isinstance(amount, int | float) and amount > 0 else None

    # Курс не пересчитываем: показываем то, что написал заказчик. Пересчёт
    # требовал бы внешнего источника котировок и врал бы на глазах.
    return value, _CURRENCY_SIGNS.get(code, code)


def _web_link(item: dict[str, Any], native_id: Any) -> str:
    links = item.get("links")
    if isinstance(links, dict):
        this = links.get("self")
        if isinstance(this, dict) and this.get("web"):
            return str(this["web"])
    return f"https://freelancehunt.com/project/{native_id}.html"


def _skill(attributes: dict[str, Any]) -> str | None:
    skills = attributes.get("skills")
    if isinstance(skills, list) and skills and isinstance(skills[0], dict):
        name = skills[0].get("name")
        if name:
            return str(name)
    return None
