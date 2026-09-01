"""Роутеры бота: участник и организатор."""

from __future__ import annotations

from aiogram import Router

from ..config import Settings
from ..notify import Notify
from ..storage import Storage
from .admin import admin_commands, build_admin_router
from .client import build_client_router


def build_router(storage: Storage, notify: Notify, settings: Settings) -> Router:
    root = Router(name="root")
    # Админский роутер первым: /start у организатора должен показывать
    # розыгрыш так же, как у всех, а вот /new — не уходить в клиентский.
    root.include_router(build_admin_router(storage, notify, settings))
    root.include_router(build_client_router(storage, settings))
    return root


__all__ = ["admin_commands", "build_admin_router", "build_client_router", "build_router"]
