"""Реестр источников отзывов."""

from __future__ import annotations

from .base import Source
from .demo import DemoSource
from .filesource import FileSource
from .gis2 import TwoGisSource
from .ozon import OzonSource
from .wildberries import WildberriesSource
from .yandex import YandexMapsSource

SOURCES: dict[str, Source] = {
    source.name: source
    for source in (
        YandexMapsSource(),
        TwoGisSource(),
        WildberriesSource(),
        OzonSource(),
        FileSource(),
        DemoSource(),
    )
}


def get_source(name: str) -> Source:
    try:
        return SOURCES[name.strip().lower()]
    except KeyError:
        known = ", ".join(SOURCES)
        raise KeyError(f"Неизвестный источник «{name}». Доступны: {known}") from None


def describe_sources() -> list[dict[str, object]]:
    return [source.describe() for source in SOURCES.values()]


__all__ = ["SOURCES", "Source", "get_source", "describe_sources"]
