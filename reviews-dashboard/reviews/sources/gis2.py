"""2ГИС: отзывы о филиале через публичный API виджета отзывов.

Ключ API не зашит в код: положите его в переменную окружения
``GIS2_API_KEY`` (или передайте ``--api-key``). Без ключа источник
работает только в офлайн-режиме ``--from-file`` с сохранённым JSON.
"""

from __future__ import annotations

import os
import re
from typing import Any

from ..httpclient import FetchError, fetch_json, polite_sleep
from ..models import Review
from .base import Source

API_URL = "https://public-api.2gis.com/2.0/market/branch/reviews"
BRANCH_ID_RE = re.compile(r"firm/(\d+)|branch_id=(\d+)|(?:^|/)(\d{8,})")
PAGE_SIZE = 50


def extract_branch_id(query: str) -> str:
    query = (query or "").strip()
    if query.isdigit():
        return query
    match = BRANCH_ID_RE.search(query)
    if not match:
        return ""
    return next((group for group in match.groups() if group), "")


def parse_payload_json(payload: Any, company: str) -> list[Review]:
    """Разбирает ответ API (или его кусок) в список отзывов."""
    if isinstance(payload, str):
        import json

        payload = json.loads(payload)

    if isinstance(payload, dict):
        items = payload.get("reviews")
        if items is None:
            result = payload.get("result") or {}
            items = result.get("items") or result.get("reviews") or []
    else:
        items = payload or []

    reviews: list[Review] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        user = item.get("user") or {}
        answer = item.get("official_answer") or item.get("officialAnswer") or {}
        reviews.append(
            Review(
                source="2gis",
                company=company,
                text=item.get("text") or "",
                rating=item.get("rating"),
                author=(user.get("name") if isinstance(user, dict) else "") or item.get("user_name", ""),
                date=item.get("date_created") or item.get("created_at") or item.get("date_edited") or "",
                url=item.get("url") or item.get("public_url") or "",
                external_id=str(item.get("id") or ""),
                reply=(answer.get("text") if isinstance(answer, dict) else "") or "",
            )
        )
    return reviews


class TwoGisSource(Source):
    name = "2gis"
    title = "2ГИС"
    query_hint = "ссылка на филиал или его id, например https://2gis.ru/moscow/firm/70000001234567890"
    needs_key = True

    def collect(self, query: str, limit: int = 100, **options: Any) -> list[Review]:
        branch_id = extract_branch_id(query)
        if not branch_id:
            raise FetchError(
                "Не удалось определить id филиала. Передайте ссылку вида "
                "https://2gis.ru/city/firm/70000001234567890"
            )
        api_key = options.get("api_key") or os.getenv("GIS2_API_KEY", "")
        if not api_key:
            raise FetchError(
                "Нужен ключ публичного API 2ГИС: задайте переменную окружения "
                "GIS2_API_KEY или передайте --api-key. Без ключа доступен "
                "офлайн-режим: --from-file сохранённый.json"
            )

        company = self.company_key(query, options.get("company")) or f"2gis:{branch_id}"
        collected: list[Review] = []
        offset = 0
        while len(collected) < limit:
            payload = fetch_json(
                API_URL,
                params={
                    "branch_id": branch_id,
                    "limit": min(PAGE_SIZE, limit - len(collected)),
                    "offset": offset,
                    "is_advertiser": "false",
                    "rated": "true",
                    "sort_by": "date_created",
                    "locale": "ru_RU",
                    "key": api_key,
                    "fields": "meta.branch_rating,meta.branch_reviews_count,meta.total_count",
                },
            )
            page = parse_payload_json(payload, company)
            if not page:
                break
            collected.extend(page)
            offset += len(page)
            if len(page) < PAGE_SIZE:
                break
            polite_sleep()
        return collected[:limit]

    def parse_payload(self, payload: str, company: str) -> list[Review]:
        return parse_payload_json(payload, company)
