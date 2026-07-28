"""Weblancer — лента проектов."""

from __future__ import annotations

from .rss import RssSource


class WeblancerSource(RssSource):
    slug = "weblancer"
    title = "Weblancer"
    feed_url = "https://www.weblancer.net/rss/projects/"
    # Ссылки вида /projects/sites-3/nazvanie-proekta-1234567/ — идентификатор
    # в самом конце, перед закрывающим слэшем.
    id_pattern = r"(\d+)/?$"
