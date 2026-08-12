"""Проверка ссылки до того, как сервер пойдёт по ней ходить.

Сервис открытый: ссылку присылает посторонний человек, и сервер выполняет по ней
запрос от своего имени, изнутри сети хостера. Значит, ссылка — это чужой ввод в
самом опасном виде, и относиться к ней надо соответственно.

Главное, от чего защищаемся, — SSRF: попытка заставить сервер постучаться туда,
куда снаружи хода нет. У облачных провайдеров по адресу 169.254.169.254 лежит
служба метаданных с ключами доступа, в соседних подсетях — админки без пароля,
на localhost — база самого сайта. Поэтому имя разрешается в адреса заранее, и
если хоть один адрес оказывается непубличным, ссылка отклоняется целиком.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MAX_LENGTH = 2048

# Схема в начале строки: буква, дальше буквы, цифры и +-. — и двоеточие.
SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")

# Параметры, которые ничего не значат для скачивания, зато тащат за собой
# плейлист целиком или чужую метку рекламной кампании.
DROP_PARAMS = frozenset(
    {
        "list",
        "start_radio",
        "index",
        "pp",
        "feature",
        "si",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "fbclid",
        "gclid",
    }
)


class UrlError(ValueError):
    """Ссылка не годится. Текст показывается человеку как есть."""


@dataclass(frozen=True, slots=True)
class Target:
    """Разобранная и проверенная ссылка."""

    url: str
    host: str
    site: str


def clean(raw: str) -> str:
    """Привести ссылку к виду, пригодному для разбора.

    Люди присылают ссылку из буфера как есть: с пробелами по краям, в угловых
    скобках из мессенджера, иногда вовсе без схемы.
    """
    text = (raw or "").strip().strip("<>").strip()
    if not text:
        raise UrlError("пустая ссылка")
    if len(text) > MAX_LENGTH:
        raise UrlError("ссылка неправдоподобно длинная")
    if "\n" in text or "\r" in text:
        raise UrlError("в ссылке перенос строки — похоже, скопировалось лишнее")
    if "://" not in text:
        # Схему дописываем только тому, у кого её нет вовсе: «youtube.com/watch»
        # человек и правда имел в виду как https. А вот «javascript:alert(1)» и
        # «file:/etc/passwd» схему уже указали — им она и останется, чтобы
        # проверка ниже отклонила их за неё, а не пропустила как чужое имя хоста.
        if SCHEME.match(text):
            raise UrlError("поддерживаются только ссылки http и https")
        text = "https://" + text
    return text


def check(raw: str, *, resolve: bool = True) -> Target:
    """Разобрать ссылку и убедиться, что по ней можно идти.

    `resolve=False` выключает обращение к DNS — нужно тестам и тем случаям,
    когда имя всё равно будет разрешать сам `yt-dlp` через прокси.
    """
    text = clean(raw)
    parts = urlsplit(text)

    if parts.scheme not in ("http", "https"):
        raise UrlError("поддерживаются только ссылки http и https")
    host = (parts.hostname or "").strip().rstrip(".")
    if not host:
        raise UrlError("в ссылке нет адреса сайта")
    if parts.username or parts.password:
        raise UrlError("ссылка с логином и паролем не принимается")

    if resolve:
        _forbid_private(host)

    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key.lower() not in DROP_PARAMS]
    normalised = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), "")
    )
    return Target(url=normalised, host=host, site=site_of(host))


def site_of(host: str) -> str:
    """Короткое имя площадки для карточки: youtube.com, vimeo.com и так далее."""
    name = host.lower()
    if name.startswith("www."):
        name = name[4:]
    if name.startswith("m."):
        name = name[2:]
    return name


def _forbid_private(host: str) -> None:
    """Отклонить всё, что ведёт внутрь сети, а не наружу."""
    literal = _as_ip(host)
    addresses = [literal] if literal else _resolve(host)
    for address in addresses:
        if not _is_public(address):
            raise UrlError(
                "эта ссылка ведёт внутрь сети сервера, а не в интернет — "
                "такие адреса сервис не открывает"
            )


def _as_ip(host: str) -> ipaddress._BaseAddress | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def _resolve(host: str) -> list[ipaddress._BaseAddress]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as error:
        raise UrlError(f"не нашёлся такой сайт: {host}") from error
    except UnicodeError as error:  # слишком длинное или кривое имя
        raise UrlError(f"неправильное имя сайта: {host}") from error
    found = []
    for info in infos:
        with_port = info[4][0]
        address = _as_ip(with_port)
        if address is not None:
            found.append(address)
    if not found:
        raise UrlError(f"не нашёлся такой сайт: {host}")
    return found


def _is_public(address: ipaddress._BaseAddress) -> bool:
    """Публичный ли адрес.

    `is_global` из стандартной библиотеки закрывает основное, но не всё:
    IPv4-адрес, завёрнутый в IPv6 (::ffff:127.0.0.1), она считает глобальным.
    Поэтому такие адреса сначала разворачиваются обратно.
    """
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return _is_public(address.ipv4_mapped)
        if address.sixtofour is not None:
            return _is_public(address.sixtofour)
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
    )


__all__ = ["Target", "UrlError", "check", "clean", "site_of"]
