"""Kwork — биржа без публичного API.

Страница проектов отрисовывается на клиенте, а данные приезжают вместе с HTML
во встроенном JSON-стейте. Разбираем его, потому что это и точнее, и стабильнее
вычитывания вёрстки. Если стейт не нашёлся (Kwork меняет фронтенд), включается
запасной разбор карточек — заказы приедут, пусть и с меньшим числом полей.

Когда сломается и запасной путь, чинить так:

    python -m fparser probe kwork      # сохранит живую страницу в tests/fixtures/
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

from lxml import html as lxml_html

from ..models import Order
from .base import Fetcher, Source, extract_budget, strip_html

log = logging.getLogger(__name__)

BASE_URL = "https://kwork.ru"

# Известные имена переменных со стейтом. Порядок — от более специфичного к общему.
_STATE_VARIABLES = (
    "window.stateData",
    "window.__INITIAL_STATE__",
    "window.__NUXT__",
    "window.initialState",
)

_PROJECT_URL_RE = re.compile(r"/projects/(\d+)")

_ID_KEYS = ("id", "want_id", "project_id", "wantId")
_TITLE_KEYS = ("name", "title", "want_name", "header")
# Без одного из этих ключей объект — почти наверняка не заказ, а категория или
# пользователь: у них тоже есть id и name, и без проверки они попадут в ленту.
_MARKER_KEYS = (
    "description",
    "desc",
    "price",
    "priceLimit",
    "price_limit",
    "budget",
    "date_create",
    "dateCreate",
    "created_at",
    "offers",
    "offers_count",
    "kwork_count",
)
_PRICE_MIN_KEYS = ("price", "price_from", "priceFrom", "budget_min", "possible_price_limit")
_PRICE_MAX_KEYS = ("priceLimit", "price_limit", "price_to", "priceTo", "budget_max")
_OFFERS_KEYS = ("offers", "offers_count", "offersCount", "kwork_count")
_DATE_KEYS = ("date_create", "dateCreate", "created_at", "date_confirm", "time_left")


class KworkSource(Source):
    slug = "kwork"
    title = "Kwork"
    feed_url = f"{BASE_URL}/projects"

    async def fetch(self, fetcher: Fetcher) -> list[Order]:
        response = await fetcher.get(self.feed_url, headers={"Referer": BASE_URL + "/"})
        return self.parse(response.text)

    def parse(self, page: str) -> list[Order]:
        raw_projects = extract_state_projects(page)
        if raw_projects:
            orders = [self._order_from_json(item) for item in raw_projects]
            orders = [order for order in orders if order is not None]
            if orders:
                log.debug("kwork: из JSON-стейта разобрано %d заказов", len(orders))
                return orders  # type: ignore[return-value]

        log.warning(
            "kwork: не удалось прочитать JSON-стейт, разбираю вёрстку. "
            "Если заказов мало или нет — сохрани страницу через `python -m fparser probe kwork`."
        )
        return parse_cards(page)

    def _order_from_json(self, item: dict[str, Any]) -> Order | None:
        native_id = _first_str(item, _ID_KEYS)
        title = _first_str(item, _TITLE_KEYS)
        if not native_id or not title:
            return None

        description = strip_html(str(item.get("description") or item.get("desc") or ""))
        budget_min = _first_int(item, _PRICE_MIN_KEYS)
        budget_max = _first_int(item, _PRICE_MAX_KEYS)
        if budget_min and budget_max and budget_min > budget_max:
            budget_min, budget_max = budget_max, budget_min

        url = item.get("url") or item.get("link") or f"{BASE_URL}/projects/{native_id}"

        return Order(
            source="kwork",
            native_id=native_id,
            title=strip_html(title),
            url=urljoin(BASE_URL, str(url)),
            description=description,
            budget_min=budget_min,
            budget_max=budget_max,
            category=_extract_category(item),
            published_at=_parse_timestamp(item),
            responses_count=_first_int(item, _OFFERS_KEYS),
        )


# ──────────────────────────────────────────────────────────────────────────────
# JSON-стейт
# ──────────────────────────────────────────────────────────────────────────────


def extract_state_projects(page: str) -> list[dict[str, Any]]:
    """Найти в HTML встроенный JSON и вытащить из него список заказов."""
    for variable in _STATE_VARIABLES:
        position = page.find(variable)
        if position == -1:
            continue

        brace = page.find("{", position)
        if brace == -1:
            continue

        blob = _balanced_json(page, brace)
        if not blob:
            continue

        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            log.debug("kwork: %s найден, но не разобрался как JSON", variable)
            continue

        projects = _find_project_list(data)
        if projects:
            return projects

    return []


def _balanced_json(text: str, start: int) -> str | None:
    """Вырезать сбалансированный объект `{...}`, начиная с позиции `start`.

    Регуляркой это сделать нельзя: внутри стейта есть вложенные объекты, а в
    строках — фигурные скобки и экранированные кавычки из пользовательских
    описаний. Поэтому считаем глубину, честно пропуская содержимое строк.
    """
    depth = 0
    in_string = False
    quote = ""
    escaped = False

    for index in range(start, len(text)):
        char = text[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                in_string = False
            continue

        if char in ('"', "'"):
            in_string = True
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    return None


def _find_project_list(data: Any) -> list[dict[str, Any]]:
    """Обойти стейт и найти самый длинный список объектов, похожих на заказы.

    Kwork не раз переименовывал ключ, в котором лежат проекты, поэтому вместо
    жёсткого пути ищем по форме данных — так парсер переживает переезды.
    """
    best: list[dict[str, Any]] = []
    stack: list[Any] = [data]

    while stack:
        node = stack.pop()

        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            candidates = [entry for entry in node if _looks_like_project(entry)]
            if len(candidates) > len(best):
                best = candidates
            stack.extend(entry for entry in node if isinstance(entry, dict | list))

    return best


def _looks_like_project(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    has_id = any(key in entry for key in _ID_KEYS)
    has_title = any(key in entry for key in _TITLE_KEYS)
    has_marker = any(key in entry for key in _MARKER_KEYS)
    return has_id and has_title and has_marker


def _first_str(item: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str | int) and str(value).strip():
            return str(value).strip()
    return None


def _first_int(item: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float) and value > 0:
            return int(value)
        if isinstance(value, str):
            digits = re.sub(r"[^\d]", "", value)
            if digits:
                return int(digits)
    return None


def _extract_category(item: dict[str, Any]) -> str | None:
    category = item.get("category") or item.get("category_name") or item.get("categoryName")
    if isinstance(category, dict):
        return _first_str(category, ("name", "title"))
    if isinstance(category, str) and category.strip():
        return category.strip()
    return None


def _parse_timestamp(item: dict[str, Any]) -> datetime | None:
    for key in _DATE_KEYS:
        value = item.get(key)
        if isinstance(value, int | float) and value > 1_000_000_000:
            return datetime.fromtimestamp(value, tz=UTC)
        if isinstance(value, str) and value.strip():
            raw = value.strip().replace(" ", "T", 1).replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError:
                continue
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Запасной разбор вёрстки
# ──────────────────────────────────────────────────────────────────────────────


def parse_cards(page: str) -> list[Order]:
    """Собрать заказы из HTML, когда JSON-стейт недоступен.

    Опираемся на единственное, что у карточки заказа не может измениться, —
    ссылку вида /projects/<id>. Классы вёрстки намеренно не используем: они
    переписываются каждый редизайн, а ссылка на заказ — нет.
    """
    try:
        document = lxml_html.fromstring(page)
    except Exception:
        # Битый HTML — не повод ронять весь опрос.
        log.exception("kwork: не удалось разобрать HTML")
        return []

    orders: dict[str, Order] = {}

    for link in document.xpath("//a[contains(@href, '/projects/')]"):
        href = link.get("href") or ""
        match = _PROJECT_URL_RE.search(href)
        if not match:
            continue

        title = strip_html(link.text_content())
        # Навигация и хлебные крошки тоже ведут на /projects/, но у них короткий
        # текст вроде «Все проекты» — заголовок заказа так не выглядит.
        if len(title) < 12:
            continue

        native_id = match.group(1)
        if native_id in orders:
            continue

        budget_min, budget_max = extract_budget(_card_text(link))

        orders[native_id] = Order(
            source="kwork",
            native_id=native_id,
            title=title,
            url=urljoin(BASE_URL, href),
            description="",
            budget_min=budget_min,
            budget_max=budget_max,
        )

    return list(orders.values())


def _card_text(link) -> str:
    """Текст карточки вокруг ссылки — там ищем бюджет.

    Поднимаемся вверх ровно до тех пор, пока в поддереве остаётся одна ссылка
    на заказ. Иначе легко подняться до списка целиком и приписать заказу цену
    соседа — ошибка тем неприятнее, что выглядит правдоподобно.
    """
    node = link
    for _ in range(4):
        parent = node.getparent()
        if parent is None or _count_project_links(parent) > 1:
            break
        node = parent
    return strip_html(node.text_content())


def _count_project_links(node) -> int:
    return sum(
        1
        for anchor in node.xpath(".//a[contains(@href, '/projects/')]")
        if _PROJECT_URL_RE.search(anchor.get("href") or "")
    )
