"""Роутеры бота: подписчик и админка воронки."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from aiogram import Router

from ..broadcast import Broadcaster
from ..config import Settings
from ..delivery import Deliverer
from ..storage import Storage
from .admin import build_admin_router
from .client import build_client_router


def build_router(
    storage: Storage,
    deliverer: Deliverer,
    broadcaster: Broadcaster,
    settings: Settings,
    notify_owner: Callable[[str], Awaitable[None]] | None = None,
) -> Router:
    root = Router(name="root")
    # Админский роутер первым: команды владельца не должны попадать в разбор
    # кодовых слов, где любой текст считается попыткой получить материал.
    root.include_router(build_admin_router(storage, broadcaster, settings))
    root.include_router(build_client_router(storage, deliverer, settings, notify_owner))
    return root


__all__ = ["build_admin_router", "build_client_router", "build_router"]
