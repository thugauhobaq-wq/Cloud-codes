"""Модели: сайт-клиент и отзыв.

Тут же вся валидация входящих данных — сервер, бот и импорт из CLI
складывают отзывы через одни и те же функции, чтобы в базу не попало
ничего кривого.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import secrets
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

MAX_TEXT = 2000
MIN_TEXT = 8
MAX_AUTHOR = 60
MAX_CONTACT = 120
MAX_REPLY = 1000

SOURCES = ("form", "telegram", "import", "demo")
STATUSES = ("pending", "published", "rejected")

#: Настройки внешнего вида виджета. Хранятся у сайта, отдаются в API и
#: подставляются в `/w/<key>.js`, чтобы клиенту не пришлось менять строчку
#: кода на своей странице ради смены темы.
DEFAULT_SETTINGS: dict[str, Any] = {
    "title": "Отзывы клиентов",
    "theme": "auto",              # light | dark | auto (по системной теме)
    "accent": "#2f6df6",
    "layout": "carousel",         # carousel | grid
    "limit": 20,                  # сколько отзывов отдавать виджету
    "min_rating": 1,              # отзывы ниже этой оценки не показываем
    "autoplay": True,
    "interval": 6000,             # мс между автопрокрутками
    "show_summary": True,         # шапка со средней оценкой
    "show_form_button": True,     # кнопка «Оставить отзыв»
    "form_button_text": "Оставить отзыв",
    "card_min_width": 300,        # от неё зависит, сколько карточек в ряду
    "radius": 16,
    "seo_schema": False,          # разметка schema.org AggregateRating
}

BOOL_SETTINGS = ("autoplay", "show_summary", "show_form_button", "seo_schema")
INT_SETTINGS = {
    "limit": (1, 100),
    "min_rating": (1, 5),
    "interval": (2000, 60000),
    "card_min_width": (220, 520),
    "radius": (0, 32),
}
ENUM_SETTINGS = {
    "theme": ("auto", "light", "dark"),
    "layout": ("carousel", "grid"),
}

_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_SPACE_RE = re.compile(r"[ \t ]+")
_URL_RE = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)


class ValidationError(ValueError):
    """Данные не прошли проверку — текст сообщения показываем пользователю."""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_key(prefix: str = "s") -> str:
    """Короткий публичный идентификатор сайта: попадает в URL виджета."""
    return f"{prefix}_{secrets.token_hex(5)}"


def new_token() -> str:
    """Секрет для входа в админку конкретного сайта."""
    return secrets.token_urlsafe(24)


def clean_text(value: Any, limit: int) -> str:
    """Убирает разметку, невидимые символы и лишние переводы строк."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = re.sub(r"<[^>]*>", " ", text)
    text = html.unescape(text)
    # Управляющие символы и «нулевой ширины» — любимый инструмент спамеров.
    text = "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch)[0] != "C")
    text = text.replace("​", "").replace("﻿", "")
    text = _SPACE_RE.sub(" ", text.replace("\r\n", "\n").replace("\r", "\n"))
    # После вырезания тегов остаются пробелы перед запятыми — «сервис , спасибо».
    text = re.sub(r" +([,.!?;:…])", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.strip() for line in text.split("\n")).strip()
    return text[:limit].strip()


def initials(author: str) -> str:
    parts = [p for p in re.split(r"[\s.]+", author) if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][:1] + parts[1][:1]).upper()


def parse_rating(value: Any) -> int:
    try:
        rating = int(round(float(str(value).replace(",", "."))))
    except (TypeError, ValueError):
        raise ValidationError("Оценка должна быть числом от 1 до 5") from None
    if not 1 <= rating <= 5:
        raise ValidationError("Оценка должна быть от 1 до 5")
    return rating


def normalize_settings(raw: Any, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Склеивает пришедшие настройки с дефолтными, отбрасывая мусор."""
    settings = dict(base or DEFAULT_SETTINGS)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    if not isinstance(raw, dict):
        return settings

    for key, value in raw.items():
        if key not in DEFAULT_SETTINGS:
            continue
        if key in BOOL_SETTINGS:
            settings[key] = value in (True, 1, "1", "true", "on", "yes")
        elif key in INT_SETTINGS:
            low, high = INT_SETTINGS[key]
            try:
                settings[key] = max(low, min(high, int(float(value))))
            except (TypeError, ValueError):
                continue
        elif key in ENUM_SETTINGS:
            if str(value) in ENUM_SETTINGS[key]:
                settings[key] = str(value)
        elif key == "accent":
            colour = str(value).strip()
            if _COLOR_RE.match(colour):
                settings[key] = colour.lower()
        else:
            settings[key] = clean_text(value, 120)
    return settings


def normalize_domains(value: Any) -> list[str]:
    """Список доменов, которым разрешено слать отзывы (проверка Origin)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = [part for part in re.split(r"[,\s]+", value) if part]
    if not isinstance(value, (list, tuple)):
        return []
    domains = []
    for item in value:
        host = clean_text(item, 120).lower()
        host = re.sub(r"^https?://", "", host).strip("/")
        host = host.split("/")[0]
        if host and host not in domains:
            domains.append(host)
    return domains


@dataclass
class Site:
    """Клиент виджета: один сайт — один ключ."""

    key: str
    name: str
    admin_token: str = field(default_factory=new_token)
    domains: list[str] = field(default_factory=list)
    auto_approve: bool = False
    auto_approve_from: int = 5      # с какой оценки публиковать без модерации
    telegram_token: str = ""
    telegram_chat: str = ""         # куда слать уведомления о новых отзывах
    settings: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_SETTINGS))
    created_at: str = field(default_factory=now_iso)

    @classmethod
    def create(cls, name: str, **kwargs: Any) -> "Site":
        name = clean_text(name, 80) or "Мой сайт"
        return cls(key=new_key(), name=name, **kwargs)

    def public(self) -> dict[str, Any]:
        return {"key": self.key, "name": self.name, "settings": self.settings}

    def allows_origin(self, origin: str) -> bool:
        """Пустой список доменов = «принимаем откуда угодно» (режим отладки)."""
        if not self.domains:
            return True
        host = re.sub(r"^https?://", "", (origin or "").strip()).split("/")[0].lower()
        host = host.split(":")[0]
        if not host:
            return False
        return any(host == d or host.endswith("." + d) for d in self.domains)


@dataclass
class Review:
    """Отзыв. В базу попадает со статусом `pending`, если нет автопубликации."""

    site: str
    text: str
    rating: int
    author: str = "Аноним"
    contact: str = ""
    source: str = "form"
    status: str = "pending"
    reply: str = ""
    pinned: bool = False
    created_at: str = field(default_factory=now_iso)
    published_at: str = ""
    uid: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    id: int | None = None

    def __post_init__(self) -> None:
        if not self.uid:
            self.uid = self.make_uid()

    def make_uid(self) -> str:
        """Отпечаток для дедупликации: один и тот же текст не задвоится."""
        normalized = re.sub(r"\W+", "", self.text.lower())
        raw = f"{self.site}|{self.author.lower()}|{normalized}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def build(
        cls,
        site: str,
        text: Any,
        rating: Any,
        author: Any = "",
        contact: Any = "",
        source: str = "form",
        meta: dict[str, Any] | None = None,
    ) -> "Review":
        """Единственный вход для внешних данных: чистит и проверяет поля."""
        clean = clean_text(text, MAX_TEXT)
        if len(clean) < MIN_TEXT:
            raise ValidationError(f"Отзыв слишком короткий, напишите хотя бы {MIN_TEXT} символов")
        name = clean_text(author, MAX_AUTHOR) or "Аноним"
        if _URL_RE.search(name):
            raise ValidationError("В имени не должно быть ссылок")
        return cls(
            site=site,
            text=clean,
            rating=parse_rating(rating),
            author=name,
            contact=clean_text(contact, MAX_CONTACT),
            source=source if source in SOURCES else "form",
            meta=meta or {},
        )

    def public(self) -> dict[str, Any]:
        """То, что уходит в виджет: без контактов и служебных полей."""
        return {
            "id": self.id,
            "author": self.author,
            "initials": initials(self.author),
            "rating": self.rating,
            "text": self.text,
            "date": self.published_at or self.created_at,
            "source": self.source,
            "reply": self.reply,
            "pinned": bool(self.pinned),
        }

    def admin(self) -> dict[str, Any]:
        data = self.public()
        data.update({
            "contact": self.contact,
            "status": self.status,
            "created_at": self.created_at,
            "meta": self.meta,
        })
        return data
