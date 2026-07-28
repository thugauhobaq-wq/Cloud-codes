"""Habr Freelance (бывший Фрилансим) — лента задач."""

from __future__ import annotations

from .rss import RssSource


class HabrSource(RssSource):
    slug = "habr"
    title = "Habr Freelance"
    feed_url = "https://freelance.habr.com/tasks.rss"
    id_pattern = r"/tasks/(\d+)"
