"""Яндекс.Карты: отзывы об организации.

Карты отдают отзывы либо в разметке страницы (микроформат schema.org),
либо блоком JSON-LD. Разбор вынесен в чистые функции — их можно применить
и к живой странице, и к заранее сохранённому HTML (``--from-file``),
что важно, потому что Карты активно защищаются от роботов.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any

from ..httpclient import FetchError, fetch
from ..models import Review
from .base import Source

ORG_ID_RE = re.compile(r"/org/[^/]+/(\d+)|(?:^|/)(\d{6,})")


def extract_org_id(query: str) -> str:
    """Достаёт id организации из ссылки вида /maps/org/name/123456789/."""
    query = (query or "").strip()
    if query.isdigit():
        return query
    match = ORG_ID_RE.search(query)
    if not match:
        return ""
    return match.group(1) or match.group(2) or ""


class _ReviewHTMLParser(HTMLParser):
    """Собирает отзывы из блоков ``business-review-view``."""

    BLOCK = "business-review-view"

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.reviews: list[dict[str, Any]] = []
        self._depth = 0
        self._current: dict[str, Any] | None = None
        self._field: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: (value or "") for key, value in attrs}
        classes = attributes.get("class", "")

        if self._current is None:
            if self.BLOCK in classes and "__" not in classes.split()[0]:
                self._current = {"author": "", "text": "", "date": "",
                                 "rating": None, "stars": 0, "reply": "", "url": ""}
                self._depth = 1
            return

        self._depth += 1

        itemprop = attributes.get("itemprop", "")
        if tag == "meta" and itemprop:
            content = attributes.get("content", "")
            if itemprop == "ratingValue":
                self._current["rating"] = _to_float(content)
            elif itemprop == "datePublished":
                self._current["date"] = content
            elif itemprop == "name" and not self._current["author"]:
                self._current["author"] = content
            self._depth -= 1  # meta не имеет закрывающего тега
            return

        if tag == "a" and "href" in attributes and not self._current["url"]:
            href = attributes["href"]
            if "/reviews" in href or "/org/" in href:
                self._current["url"] = href

        if "_full" in classes and "star" in classes:
            self._current["stars"] += 1

        if f"{self.BLOCK}__author-name" in classes or itemprop == "name":
            self._field = "author"
        elif f"{self.BLOCK}__body-text" in classes or itemprop == "reviewBody":
            self._field = "text"
        elif f"{self.BLOCK}__date" in classes or itemprop == "datePublished":
            self._field = "date"
        elif "business-review-comment" in classes or "official-answer" in classes:
            self._field = "reply"

    def handle_endtag(self, tag: str) -> None:
        if self._current is None:
            return
        self._depth -= 1
        self._field = None
        if self._depth <= 0:
            if self._current["rating"] is None and self._current["stars"]:
                self._current["rating"] = float(min(5, self._current["stars"]))
            if self._current["text"] or self._current["rating"]:
                self.reviews.append(self._current)
            self._current = None

    def handle_data(self, data: str) -> None:
        if self._current is None or not self._field:
            return
        chunk = data.strip()
        if not chunk:
            return
        existing = self._current[self._field]
        self._current[self._field] = f"{existing} {chunk}".strip() if existing else chunk


def _to_float(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def parse_jsonld(html: str) -> list[dict[str, Any]]:
    """Отзывы из блоков ``application/ld+json`` (schema.org Review)."""
    found: list[dict[str, Any]] = []
    for raw in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        try:
            payload = json.loads(raw.strip())
        except json.JSONDecodeError:
            continue
        for node in payload if isinstance(payload, list) else [payload]:
            if not isinstance(node, dict):
                continue
            reviews = node.get("review") or node.get("reviews") or []
            if isinstance(reviews, dict):
                reviews = [reviews]
            for item in reviews:
                if not isinstance(item, dict):
                    continue
                rating = item.get("reviewRating") or {}
                author = item.get("author") or {}
                found.append({
                    "author": author.get("name", "") if isinstance(author, dict) else str(author),
                    "text": item.get("reviewBody") or item.get("description") or "",
                    "date": item.get("datePublished") or "",
                    "rating": _to_float(rating.get("ratingValue")) if isinstance(rating, dict) else None,
                    "reply": "",
                    "url": item.get("url") or "",
                })
    return found


def parse_html(html: str) -> list[dict[str, Any]]:
    """Пробует оба формата и возвращает то, где отзывов больше."""
    parser = _ReviewHTMLParser()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 — битый HTML не должен ронять сбор
        pass
    from_markup = parser.reviews
    from_jsonld = parse_jsonld(html)
    return from_markup if len(from_markup) >= len(from_jsonld) else from_jsonld


class YandexMapsSource(Source):
    name = "yandex"
    title = "Яндекс.Карты"
    query_hint = "ссылка на организацию или её id, например https://yandex.ru/maps/org/.../1234567890/"
    needs_key = False

    def collect(self, query: str, limit: int = 100, **options: Any) -> list[Review]:
        org_id = extract_org_id(query)
        if not org_id:
            raise FetchError(
                "Не удалось определить id организации. Передайте ссылку вида "
                "https://yandex.ru/maps/org/название/1234567890/reviews/"
            )
        company = self.company_key(query, options.get("company")) or f"yandex:{org_id}"
        url = f"https://yandex.ru/maps/org/{org_id}/reviews/"
        html = fetch(url, headers={"Accept": "text/html,application/xhtml+xml"})
        items = parse_html(html)
        if not items:
            raise FetchError(
                "Яндекс.Карты не отдали отзывы (обычно это антибот-заглушка). "
                "Сохраните страницу отзывов из браузера и запустите с "
                "--from-file путь.html"
            )
        return self._to_reviews(items, company, url)[:limit]

    def parse_payload(self, payload: str, company: str) -> list[Review]:
        return self._to_reviews(parse_html(payload), company, "")

    @staticmethod
    def _to_reviews(items: list[dict[str, Any]], company: str, url: str) -> list[Review]:
        reviews = []
        for index, item in enumerate(items):
            link = item.get("url") or url
            if link.startswith("/"):
                link = f"https://yandex.ru{link}"
            reviews.append(
                Review(
                    source="yandex",
                    company=company,
                    text=item.get("text", ""),
                    rating=item.get("rating"),
                    author=item.get("author", ""),
                    date=item.get("date", ""),
                    url=link,
                    reply=item.get("reply", ""),
                    external_id=f"{company}-{index}-{(item.get('text') or '')[:40]}",
                )
            )
        return reviews
