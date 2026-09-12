"""Реестр площадок."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .base import Fetcher, FetchError, Source
from .flru import FlRuSource
from .freelancehunt import FreelancehuntSource
from .habr import HabrSource
from .hh import HhSource
from .kwork import KworkSource
from .rss import RssSource
from .telegram import TelegramChannelsSource
from .weblancer import WeblancerSource

SOURCE_CLASSES: tuple[type[Source], ...] = (
    KworkSource,
    FlRuSource,
    HabrSource,
    WeblancerSource,
    TelegramChannelsSource,
    HhSource,
    FreelancehuntSource,
)

ALL_SLUGS: tuple[str, ...] = tuple(cls.slug for cls in SOURCE_CLASSES)


def build_sources(
    url_overrides: dict[str, str | None] | None = None,
    *,
    channels_provider: Callable[[], Awaitable[list[str]]] | None = None,
    hh_query: str | None = None,
    freelancehunt_token: str | None = None,
) -> list[Source]:
    """Создать все источники.

    Собираем список явно, а не циклом по классам: источникам нужны разные
    зависимости — каналы из БД, строка поиска, токен, — и перечислить их
    по именам понятнее любой обобщённой схемы.

    `url_overrides` позволяет подменить адрес через окружение. Это не
    украшательство: площадки меняют адреса лент, и возможность починить парсер
    строчкой в `.env` экономит выкладку новой версии.

    Источники, которым не хватает настроек, всё равно создаются и объясняют
    это при опросе. Так `ALL_SLUGS` остаётся постоянным — на него завязаны
    фильтры, клавиатуры бота и выбор в CLI, — а `dryrun` сам показывает,
    чего не хватает.
    """
    overrides = url_overrides or {}
    return [
        KworkSource(feed_url=overrides.get("kwork")),
        FlRuSource(feed_url=overrides.get("flru")),
        HabrSource(feed_url=overrides.get("habr")),
        WeblancerSource(feed_url=overrides.get("weblancer")),
        TelegramChannelsSource(
            feed_url=overrides.get("tg"), channels_provider=channels_provider
        ),
        HhSource(feed_url=overrides.get("hh"), query=hh_query),
        FreelancehuntSource(feed_url=overrides.get("fh"), token=freelancehunt_token),
    ]


def source_titles() -> dict[str, str]:
    return {cls.slug: cls.title for cls in SOURCE_CLASSES}


__all__ = [
    "ALL_SLUGS",
    "SOURCE_CLASSES",
    "FetchError",
    "Fetcher",
    "FlRuSource",
    "FreelancehuntSource",
    "HabrSource",
    "HhSource",
    "KworkSource",
    "RssSource",
    "Source",
    "TelegramChannelsSource",
    "WeblancerSource",
    "build_sources",
    "source_titles",
]
