"""Wildberries: отзывы о товаре по артикулу.

Схема та же, что использует сайт: карточка товара отдаёт ``root`` (imtId),
по нему открытый сервис отзывов возвращает JSON без ключей и авторизации.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..httpclient import FetchError, fetch_json
from ..models import Review
from .base import Source

CARD_URL = "https://card.wb.ru/cards/v2/detail"
FEEDBACK_HOSTS = ("feedbacks1.wb.ru", "feedbacks2.wb.ru")
ARTICLE_RE = re.compile(r"catalog/(\d+)|(?:^|[^\d])(\d{6,12})(?:[^\d]|$)")


def extract_article(query: str) -> str:
    query = (query or "").strip()
    if query.isdigit():
        return query
    match = ARTICLE_RE.search(query)
    if not match:
        return ""
    return next((group for group in match.groups() if group), "")


def parse_feedbacks(payload: Any, company: str) -> list[Review]:
    """Разбирает ответ feedbacks*.wb.ru в общий формат."""
    if isinstance(payload, str):
        payload = json.loads(payload)
    items = payload.get("feedbacks") if isinstance(payload, dict) else payload
    if not items:
        return []

    reviews: list[Review] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        user = item.get("wbUserDetails") or {}
        answer = item.get("answer") or {}
        reviews.append(
            Review(
                source="wildberries",
                company=company,
                text=item.get("text") or "",
                pros=item.get("pros") or "",
                cons=item.get("cons") or "",
                rating=item.get("productValuation"),
                author=(user.get("name") if isinstance(user, dict) else "") or item.get("wbUserId", ""),
                date=item.get("createdDate") or item.get("updatedDate") or "",
                external_id=str(item.get("id") or ""),
                reply=(answer.get("text") if isinstance(answer, dict) else "") or "",
            )
        )
    return reviews


class WildberriesSource(Source):
    name = "wildberries"
    title = "Wildberries"
    query_hint = "артикул товара или ссылка, например https://www.wildberries.ru/catalog/145766453/detail.aspx"
    needs_key = False

    def collect(self, query: str, limit: int = 100, **options: Any) -> list[Review]:
        article = extract_article(query)
        if not article:
            raise FetchError("Не удалось определить артикул товара Wildberries.")

        card = fetch_json(
            CARD_URL,
            params={"appType": 1, "curr": "rub", "dest": -1257786, "nm": article},
        )
        products = ((card or {}).get("data") or {}).get("products") or []
        if not products:
            raise FetchError(f"Wildberries не нашёл товар с артикулом {article}.")

        product = products[0]
        imt_id = product.get("root")
        company = self.company_key(query, options.get("company")) or product.get("name") or f"wb:{article}"
        if not imt_id:
            raise FetchError("В карточке товара нет imtId — отзывы недоступны.")

        errors = []
        for host in FEEDBACK_HOSTS:
            try:
                payload = fetch_json(f"https://{host}/feedbacks/v1/{imt_id}")
            except FetchError as error:
                errors.append(str(error))
                continue
            reviews = parse_feedbacks(payload, company)
            if reviews:
                for review in reviews:
                    review.url = f"https://www.wildberries.ru/catalog/{article}/feedbacks"
                return reviews[:limit]
        raise FetchError("Отзывы Wildberries не получены: " + "; ".join(errors or ["пустой ответ"]))

    def parse_payload(self, payload: str, company: str) -> list[Review]:
        return parse_feedbacks(payload, company)
