"""Общий слой для всех площадок: HTTP с ретраями и разбор лент.

Источники (`sources/*.py`) занимаются только тем, чем реально отличаются друг от
друга — разбором конкретного формата. Сеть, ретраи, маскировка под браузер и
парсинг RSS/Atom живут здесь, чтобы не дублироваться четыре раза.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
from lxml import etree, html

from ..models import Order

log = logging.getLogger(__name__)

# Разные, но правдоподобные UA: одинаковый заголовок на каждом запросе — самый
# заметный признак бота. Список короткий намеренно, экзотика вызывает больше
# подозрений, чем обычный свежий Chrome.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
]


class FetchError(Exception):
    """Запрос не удался после всех попыток."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

    @property
    def is_blocked(self) -> bool:
        """Похоже ли на защиту от ботов, а не на случайный сбой.

        Отличать важно: при блокировке правильная реакция — увеличить паузу,
        а при 500 достаточно повторить через минуту.
        """
        return self.status_code in (403, 429)


class Fetcher:
    """Обёртка над httpx с ретраями и вежливым поведением."""

    def __init__(
        self,
        *,
        timeout: float = 25.0,
        proxy_url: str | None = None,
        max_attempts: int = 3,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._max_attempts = max_attempts
        self._own_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            proxy=proxy_url,
            headers={
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None, **kwargs
    ) -> httpx.Response:
        last_error: Exception | None = None

        # User-Agent подставляем свой, но не затираем заголовки источника:
        # Kwork, например, добавляет Referer, и конфликт аргументов раньше
        # ронял запрос ещё до отправки.
        request_headers = {"User-Agent": random.choice(USER_AGENTS)}
        if headers:
            request_headers.update(headers)

        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.get(url, headers=request_headers, **kwargs)
            except httpx.HTTPError as exc:
                last_error = FetchError(f"сетевая ошибка при запросе {url}: {exc}")
            else:
                if response.status_code < 400:
                    return response

                last_error = FetchError(
                    f"{url} ответил {response.status_code}",
                    status_code=response.status_code,
                )
                # 4xx, кроме 429, повторять бессмысленно — ответ не изменится.
                if response.status_code < 500 and response.status_code != 429:
                    raise last_error

                delay = self._retry_delay(response, attempt)
                if attempt < self._max_attempts:
                    log.warning("%s: %s, повтор через %.1f с", url, response.status_code, delay)
                    await asyncio.sleep(delay)
                continue

            if attempt < self._max_attempts:
                delay = 2.0**attempt
                log.warning("%s: %s, повтор через %.1f с", url, last_error, delay)
                await asyncio.sleep(delay)

        assert last_error is not None
        raise last_error

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        """Пауза перед повтором: слушаемся Retry-After, если сервер его прислал."""
        header = response.headers.get("Retry-After")
        if header:
            try:
                # Значение бывает и числом секунд, и датой; вторую форму не разбираем,
                # ограничиваемся разумным потолком.
                return min(float(header), 120.0)
            except ValueError:
                pass
        return 2.0**attempt

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()


class Source(ABC):
    """Одна площадка."""

    slug: str
    title: str
    #: Адрес, с которого забираются заказы. Вынесен в атрибут, чтобы смена URL
    #: на стороне биржи чинилась одной строкой или переопределением в конструкторе.
    feed_url: str

    def __init__(self, feed_url: str | None = None) -> None:
        if feed_url:
            self.feed_url = feed_url

    @abstractmethod
    async def fetch(self, fetcher: Fetcher) -> list[Order]:
        """Забрать текущую страницу/ленту заказов и привести к общей модели."""


# ──────────────────────────────────────────────────────────────────────────────
# Разбор лент
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class FeedItem:
    """Запись ленты до приведения к `Order` — общая для RSS и Atom."""

    title: str
    link: str
    description: str = ""
    guid: str | None = None
    category: str | None = None
    published_at: datetime | None = None


def _child_text(element: etree._Element, name: str) -> str | None:
    """Взять текст дочернего тега по локальному имени, игнорируя пространства имён.

    RSS-ленты бирж приходят то с namespace, то без, то с несколькими сразу.
    Поиск по local-name() избавляет от необходимости угадывать префиксы.
    """
    found = element.xpath(f"./*[local-name()='{name}']")
    for node in found:
        text = node.text
        if text and text.strip():
            return text.strip()
        # Atom кладёт ссылку в атрибут, а не в текст.
        href = node.get("href")
        if href:
            return href.strip()
    return None


def _category_text(element: etree._Element) -> str | None:
    """Рубрика записи.

    RSS кладёт её в текст `<category>`, Atom — в атрибут `term` того же тега.
    Поддерживаем оба варианта, иначе для Atom-лент рубрика всегда пустая.
    """
    for node in element.xpath("./*[local-name()='category']"):
        term = node.get("term")
        if term and term.strip():
            return term.strip()
        if node.text and node.text.strip():
            return node.text.strip()
    return None


def parse_feed(content: bytes) -> list[FeedItem]:
    """Разобрать RSS 2.0 или Atom в список записей.

    `recover=True` — осознанно: ленты бирж регулярно содержат неэкранированные
    амперсанды и битые сущности, из-за которых строгий парсер падает на всей
    ленте целиком. Лучше потерять одну запись, чем весь опрос.
    """
    parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True)
    root = etree.fromstring(content, parser=parser)
    if root is None:
        return []

    nodes = root.xpath("//*[local-name()='item'] | //*[local-name()='entry']")

    items: list[FeedItem] = []
    for node in nodes:
        title = _child_text(node, "title")
        link = _child_text(node, "link")
        if not title or not link:
            continue

        raw_description = (
            _child_text(node, "description")
            or _child_text(node, "summary")
            or _child_text(node, "content")
            or _child_text(node, "encoded")
            or ""
        )
        raw_date = (
            _child_text(node, "pubDate")
            or _child_text(node, "published")
            or _child_text(node, "updated")
            or _child_text(node, "date")
        )

        items.append(
            FeedItem(
                title=strip_html(title),
                link=link,
                description=strip_html(raw_description),
                guid=_child_text(node, "guid") or _child_text(node, "id"),
                category=_category_text(node),
                published_at=parse_date(raw_date),
            )
        )
    return items


_BREAK_RE = re.compile(r"(?i)<br\s*/?>|</(?:p|div|li|tr|h[1-6]|blockquote)>")


def strip_html(raw: str) -> str:
    """Убрать разметку и привести пробелы к нормальному виду.

    Описания в лентах приходят как HTML-фрагменты; тащить их в Telegram как есть
    нельзя — либо сломается разметка сообщения, либо пользователь увидит теги.
    """
    if not raw:
        return ""

    text = raw
    if "<" in raw:
        # Переводы строк расставляем до разбора: text_content() склеивает
        # соседние блоки вплотную, и «…в Figma.<br/>Адаптив…» превращается
        # в «…в Figma.Адаптив…».
        raw = _BREAK_RE.sub("\n", raw)
        try:
            text = html.fromstring(raw).text_content()
        except (etree.ParserError, etree.XMLSyntaxError):
            text = re.sub(r"<[^>]+>", " ", raw)

    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text.strip()


def parse_date(raw: str | None) -> datetime | None:
    """Разобрать дату из ленты: RFC 822 (RSS) или ISO 8601 (Atom)."""
    if not raw:
        return None

    raw = raw.strip()
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        parsed = None

    if parsed is None:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


# ──────────────────────────────────────────────────────────────────────────────
# Деньги и идентификаторы
# ──────────────────────────────────────────────────────────────────────────────

_NUMBER = r"\d[\d\s .]*"
_CURRENCY = r"(?:руб\.?|рублей|руб|₽|р\.)"

_RANGE_RE = re.compile(
    rf"(?:от\s*)?({_NUMBER}?\d)\s*(?:-|—|–|до)\s*({_NUMBER}?\d)\s*{_CURRENCY}",
    re.IGNORECASE,
)
_MAX_ONLY_RE = re.compile(rf"до\s*({_NUMBER}?\d)\s*{_CURRENCY}", re.IGNORECASE)
_SINGLE_RE = re.compile(rf"(?:от\s*)?({_NUMBER}?\d)\s*{_CURRENCY}", re.IGNORECASE)


def _to_int(raw: str) -> int | None:
    digits = re.sub(r"[^\d]", "", raw)
    if not digits:
        return None
    try:
        value = int(digits)
    except ValueError:
        return None
    # Отсекаем мусор вроде года в тексте или телефона, попавшего под шаблон.
    return value if 0 < value < 100_000_000 else None


def extract_budget(text: str) -> tuple[int | None, int | None]:
    """Вытащить бюджет из свободного текста.

    Ни одна из четырёх площадок не отдаёт бюджет отдельным полем в ленте —
    он написан словами в заголовке или описании («Бюджет: 5 000 руб.»),
    поэтому разбираем регулярками и спокойно возвращаем `(None, None)`,
    когда суммы нет.
    """
    if not text:
        return None, None

    match = _RANGE_RE.search(text)
    if match:
        low, high = _to_int(match.group(1)), _to_int(match.group(2))
        if low is not None and high is not None and low > high:
            low, high = high, low
        if low is not None or high is not None:
            return low, high

    # «до 120 000 руб.» — это потолок, а не гарантия. Записать его в нижнюю
    # границу значило бы пропускать такие заказы через фильтр «от 50 000»,
    # хотя заплатить по ним могут и тысячу.
    match = _MAX_ONLY_RE.search(text)
    if match:
        value = _to_int(match.group(1))
        if value is not None:
            return None, value

    match = _SINGLE_RE.search(text)
    if match:
        value = _to_int(match.group(1))
        if value is not None:
            return value, None

    return None, None


def id_from_url(url: str, pattern: str | None = None) -> str:
    """Достать идентификатор заказа из ссылки.

    Идентификатор нужен стабильный: он и есть ключ дедупликации, и если он
    «поплывёт» между опросами, один и тот же заказ прилетит дважды. Поэтому
    хэш ссылки — только последнее средство, когда числового id в URL нет.
    """
    if pattern:
        match = re.search(pattern, url)
        if match:
            return match.group(1)

    numbers = re.findall(r"(\d{3,})", url)
    if numbers:
        return numbers[-1]

    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
