"""Каркас Telegram-ботов на aiogram 3.

Всё, что повторялось от бота к боту: настройки, SQLite с транзакциями,
уведомления, рассылка, фоновые воркеры, запуск с корректной остановкой.
"""

from .broadcast import BLOCKED, FAILED, SENT, Broadcaster, Progress, Sender, telegram_sender
from .config import BotSettings
from .filters import IsAdmin, IsPrivate
from .notify import Notifier
from .runner import make_bot, run_bot, setup_logging
from .storage import BaseStorage, from_iso, now_iso, to_iso
from .worker import PeriodicWorker

__all__ = [
    "BLOCKED",
    "FAILED",
    "SENT",
    "BaseStorage",
    "BotSettings",
    "Broadcaster",
    "IsAdmin",
    "IsPrivate",
    "Notifier",
    "PeriodicWorker",
    "Progress",
    "Sender",
    "__version__",
    "from_iso",
    "make_bot",
    "now_iso",
    "run_bot",
    "setup_logging",
    "telegram_sender",
    "to_iso",
]

__version__ = "0.1.0"
