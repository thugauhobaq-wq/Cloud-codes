"""Ozon: отзывы о товаре через composer-api страницы отзывов.

Ozon жёстко фильтрует автоматические запросы, поэтому живой режим часто
упирается в защиту. Разбор ответа вынесен отдельно: сохранённый JSON
страницы (DevTools → composer-api.bx/page/json/v2) разбирается тем же кодом
через ``--from-file``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..httpclient import FetchError, fetch_json
from ..models import Review
from .base import Source

COMPOSER_URL = "https://www.ozon.ru/api/composer-api.bx/page/json/v2"
PRODUCT_RE = re.compile(r"product/([\w\-]*?(\d{6,}))|(?:^|/)(\d{6,})")


def extract_product(query: str) -> str:
    """Возвращает slug-или-id товара для url страницы отзывов."""
    query = (query or "").strip()
    if query.isdigit():
        return query
    match = re.search(r"product/([^/?#]+)", query)
    if match:
        return match.group(1)
    match = re.search(r"(\d{6,})", query)
    return match.group(1) if match else ""


def _iter_review_nodes(payload: Any) -> list[dict[str, Any]]:
    """Достаёт отзывы из widgetStates (там лежит JSON внутри строки)."""
    if isinstance(payload, str):
        payload = json.loads(payload)

    nodes: list[dict[str, Any]] = []
    states = payload.get("widgetStates") if isinstance(payload, dict) else None
    candidates: list[Any] = []
    if isinstance(states, dict):
        for key, value in states.items():
            if "review" not in key.lower():
                continue
            try:
                candidates.append(json.loads(value) if isinstance(value, str) else value)
            except json.JSONDecodeError:
                continue
    else:
        candidates.append(payload)

    for candidate in candidates:
        if isinstance(candidate, dict):
            items = candidate.get("reviews") or candidate.get("items") or []
        else:
            items = candidate or []
        for item in items:
            if isinstance(item, dict):
                nodes.append(item)
    return nodes


def parse_reviews(payload: Any, company: str) -> list[Review]:
    reviews: list[Review] = []
    for item in _iter_review_nodes(payload):
        content = item.get("content") or {}
        author = item.get("author") or {}
        text = content.get("comment") or item.get("text") or ""
        pros = content.get("positive") or item.get("advantages") or ""
        cons = content.get("negative") or item.get("disadvantages") or ""
        answers = item.get("answers") or item.get("comments") or []
        reply = ""
        if isinstance(answers, list) and answers and isinstance(answers[0], dict):
            reply = answers[0].get("text") or answers[0].get("comment") or ""

        reviews.append(
            Review(
                source="ozon",
                company=company,
                text=text,
                pros=pros,
                cons=cons,
                rating=item.get("score") or item.get("rating"),
                author=(author.get("title") or author.get("name") or "") if isinstance(author, dict) else str(author),
                date=item.get("createdAt") or item.get("publishedAt") or item.get("date") or "",
                external_id=str(item.get("uuid") or item.get("id") or ""),
                url=item.get("url") or "",
                reply=reply,
            )
        )
    return reviews


class OzonSource(Source):
    name = "ozon"
    title = "Ozon"
    query_hint = "ссылка на товар, например https://www.ozon.ru/product/nazvanie-123456789/"
    needs_key = False

    def collect(self, query: str, limit: int = 100, **options: Any) -> list[Review]:
        product = extract_product(query)
        if not product:
            raise FetchError("Не удалось определить товар Ozon по этой ссылке.")
        company = self.company_key(query, options.get("company")) or f"ozon:{product}"

        payload = fetch_json(
            COMPOSER_URL,
            params={"url": f"/product/{product}/reviews/?reviewsVariantMode=2"},
            headers={"Accept": "application/json", "Referer": f"https://www.ozon.ru/product/{product}/reviews/"},
        )
        reviews = parse_reviews(payload, company)
        if not reviews:
            raise FetchError(
                "Ozon не отдал отзывы (скорее всего сработала защита от ботов). "
                "Сохраните ответ composer-api из DevTools и запустите с --from-file."
            )
        return reviews[:limit]

    def parse_payload(self, payload: str, company: str) -> list[Review]:
        return parse_reviews(payload, company)
