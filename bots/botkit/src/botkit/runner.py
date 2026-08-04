"""Запуск бота: сборка, фоновые воркеры, корректная остановка.

Один и тот же код жил в `__main__.py` каждого бота — с сигналами, отменой
задач и закрытием соединений, которые легко забыть.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable, Sequence

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand

from .config import BotSettings
from .storage import BaseStorage
from .worker import PeriodicWorker

log = logging.getLogger(__name__)


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Штатные «Update id=… is handled» на каждый апдейт только зашумляют лог.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


def make_bot(settings: BotSettings, *, parse_mode: str = "HTML") -> Bot:
    return Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=parse_mode))


async def run_bot(
    *,
    bot: Bot,
    routers: Sequence[Router],
    workers: Sequence[PeriodicWorker] = (),
    commands: Sequence[BotCommand] = (),
    storage: BaseStorage | None = None,
    on_startup: Callable[[], Awaitable[None]] | None = None,
    on_shutdown: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Запустить бота и фоновые воркеры до сигнала остановки.

    Возвращается, когда пришёл SIGINT/SIGTERM или одна из задач упала; в
    последнем случае исключение всплывает наружу, чтобы оркестратор увидел
    ненулевой код возврата и перезапустил контейнер.
    """
    dispatcher = Dispatcher()
    for router in routers:
        dispatcher.include_router(router)

    if commands:
        with contextlib.suppress(Exception):
            # Меню команд — украшение; падать из-за него при старте не стоит.
            await bot.set_my_commands(list(commands))

    if on_startup is not None:
        await on_startup()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Без этого `docker stop` убивает процесс на полуслове: соединение с
        # базой остаётся незакрытым, а часть работы — недоделанной.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    tasks = [
        asyncio.create_task(dispatcher.start_polling(bot, handle_signals=False), name="bot"),
        asyncio.create_task(stop.wait(), name="stop"),
    ]
    tasks.extend(
        asyncio.create_task(worker.run_forever(), name=worker.name) for worker in workers
    )

    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        # Если бот или воркер упали с исключением, оно всплывёт здесь.
        for task in done:
            if task.get_name() != "stop":
                task.result()
    finally:
        log.info("останавливаюсь")
        if on_shutdown is not None:
            with contextlib.suppress(Exception):
                await on_shutdown()
        await dispatcher.storage.close()
        await bot.session.close()
        if storage is not None:
            await storage.close()
