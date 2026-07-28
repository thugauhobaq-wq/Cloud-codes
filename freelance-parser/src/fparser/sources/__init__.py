"""Реестр площадок."""

from __future__ import annotations

from .base import Fetcher, FetchError, Source
from .flru import FlRuSource
from .habr import HabrSource
from .kwork import KworkSource
from .rss import RssSource
from .weblancer import WeblancerSource

SOURCE_CLASSES: tuple[type[Source], ...] = (
    KworkSource,
    FlRuSource,
    HabrSource,
    WeblancerSource,
)

ALL_SLUGS: tuple[str, ...] = tuple(cls.slug for cls in SOURCE_CLASSES)


def build_sources(url_overrides: dict[str, str | None] | None = None) -> list[Source]:
    """Создать все источники.

    `url_overrides` позволяет подменить адрес ленты через окружение. Это не
    украшательство: биржи меняют адреса лент, и возможность починить парсер
    переменной в .env экономит выкладку новой версии.
    """
    overrides = url_overrides or {}
    return [cls(feed_url=overrides.get(cls.slug)) for cls in SOURCE_CLASSES]


def source_titles() -> dict[str, str]:
    return {cls.slug: cls.title for cls in SOURCE_CLASSES}


__all__ = [
    "ALL_SLUGS",
    "SOURCE_CLASSES",
    "FetchError",
    "Fetcher",
    "FlRuSource",
    "HabrSource",
    "KworkSource",
    "RssSource",
    "Source",
    "WeblancerSource",
    "build_sources",
    "source_titles",
]
