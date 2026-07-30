"""Роутеры бота: клиентская часть и админка."""

from __future__ import annotations

from aiogram import Router

from ..config import Settings
from ..notify import Notifier
from ..service import BookingService
from ..storage import Storage
from .admin import build_admin_router
from .client import build_client_router


def build_router(
    storage: Storage, booking: BookingService, notifier: Notifier, settings: Settings
) -> Router:
    root = Router(name="root")
    # Админский роутер идёт первым: у него свои обработчики тех же кнопок,
    # и попасть в клиентский он не должен.
    root.include_router(build_admin_router(storage, booking, notifier, settings))
    root.include_router(build_client_router(storage, booking, notifier, settings))
    return root


__all__ = ["build_admin_router", "build_client_router", "build_router"]
