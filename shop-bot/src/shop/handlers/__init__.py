"""Роутеры бота: витрина покупателя и админка продавца."""

from __future__ import annotations

from aiogram import Router

from ..config import Settings
from ..notify import Notifier
from ..storage import Storage
from .admin import build_admin_router
from .client import build_client_router


def build_router(storage: Storage, notifier: Notifier, settings: Settings) -> Router:
    root = Router(name="root")
    # Админский роутер первым: его команды не должны попадать в витрину.
    root.include_router(build_admin_router(storage, notifier, settings))
    root.include_router(build_client_router(storage, notifier, settings))
    return root


__all__ = ["build_admin_router", "build_client_router", "build_router"]
