"""Telegram-каналы с заказами.

Для ниши «боты под малый бизнес» каналы дают больше заказов, чем классические
биржи, и технически это самый простой источник: `https://t.me/s/<канал>` —
обычная HTML-страница публичного канала. Ни API, ни авторизации, ни юзербота,
ни антибота.

Каналы задаёт сам владелец командами `/tg_add` и `/tg_del`, список лежит в БД:
менять его надо с телефона наравне с остальными фильтрами, а не правкой `.env`
с перезапуском контейнера.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable

from lxml import etree
from lxml import html as lxml_html

from ..models import Order
from .base import Fetcher, FetchError, Source, extract_budget, parse_date, strip_html

log = logging.getLogger(__name__)

BASE_URL = "https://t.me"

#: Посты короче этого — подписи к картинкам, реакции и служебные сообщения.
#: Заказ так не выглядит, а в ленте они создавали бы шум.
MIN_POST_LENGTH = 30

#: Длина заголовка в сообщении. Первая строка поста бывает абзацем целиком.
TITLE_LIMIT = 120

ChannelsProvider = Callable[[], Awaitable[list[str]]]

_CHANNEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


def normalize_channel(raw: str) -> str | None:
    """Привести «@name», «t.me/name» и «https://t.me/s/name» к «name».

    Пользователь вводит имя канала как придётся — чаще всего копирует ссылку.
    Разбирать это должен код, а не человек.
    """
    value = (raw or "").strip()
    if not value:
        return None

    value = re.sub(r"^https?://", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^t\.me/", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^s/", "", value, flags=re.IGNORECASE)
    value = value.lstrip("@").strip("/")
    value = value.split("/")[0].split("?")[0]

    return value if _CHANNEL_RE.match(value) else None


def channel_url(channel: str) -> str:
    return f"{BASE_URL}/s/{channel}"


class TelegramChannelsSource(Source):
    """Все отслеживаемые каналы как одна площадка.

    Намеренно один класс на весь список, а не по источнику на канал: `ALL_SLUGS`
    завязан на реестр классов, а на него — фильтры, клавиатуры бота и выбор в
    CLI. Динамический список слагов ломал бы всё это ради косметики. Канал
    попадает в рубрику заказа, поэтому включать и выключать его можно готовой
    командой `/categories`.
    """

    slug = "tg"
    title = "Telegram-каналы"
    feed_url = BASE_URL

    def __init__(
        self,
        feed_url: str | None = None,
        channels_provider: ChannelsProvider | None = None,
    ) -> None:
        super().__init__(feed_url)
        self._channels_provider = channels_provider

    async def channels(self) -> list[str]:
        if self._channels_provider is None:
            return []
        return await self._channels_provider()

    async def fetch(self, fetcher: Fetcher) -> list[Order]:
        channels = await self.channels()
        if not channels:
            raise FetchError("не добавлено ни одного канала — команда /tg_add @канал")

        orders: list[Order] = []
        last_error: FetchError | None = None
        succeeded = 0

        for channel in channels:
            try:
                response = await fetcher.get(channel_url(channel))
            except FetchError as error:
                # Один закрытый или переименованный канал не должен лишать нас
                # остальных — их в списке обычно с десяток.
                log.warning("tg: канал @%s недоступен: %s", channel, error)
                last_error = error
                continue

            succeeded += 1
            orders.extend(parse_channel(response.text, channel))

        # Если не ответил вообще никто, отдаём ошибку наверх: поллеру нужен
        # сигнал, чтобы уйти в паузу, а не считать, что заказов просто нет.
        if succeeded == 0 and last_error is not None:
            raise last_error

        return orders


def parse_channel(page: str, channel: str) -> list[Order]:
    """Разобрать страницу `t.me/s/<канал>` в заказы."""
    try:
        document = lxml_html.fromstring(page)
    except Exception:
        # Битая страница не повод ронять опрос остальных каналов.
        log.exception("tg: не удалось разобрать страницу @%s", channel)
        return []

    orders: list[Order] = []

    for node in document.xpath("//div[contains(@class, 'tgme_widget_message') and @data-post]"):
        post = node.get("data-post") or ""
        if "/" not in post:
            continue

        text = _post_text(node)
        if len(text) < MIN_POST_LENGTH:
            continue

        title, description = _split_title(text)
        budget_min, budget_max = extract_budget(text)

        orders.append(
            Order(
                source="tg",
                # data-post уже имеет вид «канал/123» — готовый стабильный ключ.
                native_id=post,
                title=title,
                url=f"{BASE_URL}/{post}",
                description=description,
                budget_min=budget_min,
                budget_max=budget_max,
                category=f"@{channel}",
                published_at=_post_date(node),
            )
        )

    return orders


def _post_text(node) -> str:
    """Текст поста с сохранением переносов строк.

    Берём исходный HTML, а не `text_content()`: в постах переносы сделаны
    через `<br>`, и без них абзацы слипаются в одну строку.
    """
    found = node.xpath(".//div[contains(@class, 'tgme_widget_message_text')]")
    if not found:
        return ""
    raw = etree.tostring(found[0], encoding="unicode", method="html")
    return strip_html(raw)


def _post_date(node):
    for time_node in node.xpath(".//time[@datetime]"):
        parsed = parse_date(time_node.get("datetime"))
        if parsed is not None:
            return parsed
    return None


def _split_title(text: str) -> tuple[str, str]:
    """Первая содержательная строка — заголовок, остальное — описание."""
    lines = [line.strip() for line in text.split("\n")]
    lines = [line for line in lines if line]
    if not lines:
        return "", ""

    title = lines[0]
    if len(title) > TITLE_LIMIT:
        cut = title[:TITLE_LIMIT]
        space = cut.rfind(" ")
        title = (cut[:space] if space > TITLE_LIMIT // 2 else cut).rstrip(" ,.;:—-") + "…"

    return title, "\n".join(lines[1:])
