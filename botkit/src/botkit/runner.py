"""Запуск бота: сборка, фоновые воркеры, корректная остановка.

Один и тот же код жил в `__main__.py` каждого бота — с сигналами, отменой
задач и закрытием соединений, которые легко забыть.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable, Iterable, Sequence

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

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


async def _publish_commands(bot: Bot, commands, scope, label: str, *, attempts: int = 5) -> None:
    """Выставить один набор команд, повторяя при обрыве связи.

    Связь сервера с Telegram моргает: одиночный set_my_commands легко ловит
    таймаут, и тогда меню молча остаётся старым — а после перезапуска бота
    разделение команд слетает. Поэтому повторяем несколько раз.
    """
    for attempt in range(1, attempts + 1):
        try:
            await bot.set_my_commands(list(commands), scope=scope)
            return
        except Exception as exc:
            if attempt == attempts:
                # Частая причина для чата админа — он ещё не открывал бота:
                # такой чат Telegram не примет, и повторять смысла нет.
                log.warning("не выставил команды (%s): %s", label, exc)
                return
            await asyncio.sleep(2)


async def publish_commands(
    bot: Bot,
    *,
    commands: Sequence[BotCommand] = (),
    admin_commands: Sequence[BotCommand] = (),
    admins: Iterable[int] = (),
) -> None:
    """Разложить команды по областям видимости.

    Без областей Telegram показывает один список всем, и клиент видит в меню
    админские команды, которые ему всё равно недоступны, — а своих не видит.
    Поэтому общий список получают все, а админский довешивается каждому
    администратору в его личный чат.
    """
    if commands:
        await _publish_commands(bot, commands, BotCommandScopeDefault(), "клиентские")

    for admin in admins:
        if not admin_commands:
            break
        await _publish_commands(
            bot, admin_commands, BotCommandScopeChat(chat_id=admin), f"админ {admin}"
        )


async def run_bot(
    *,
    bot: Bot,
    routers: Sequence[Router],
    workers: Sequence[PeriodicWorker] = (),
    commands: Sequence[BotCommand] = (),
    admin_commands: Sequence[BotCommand] = (),
    admins: Iterable[int] = (),
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

    await publish_commands(
        bot, commands=commands, admin_commands=admin_commands, admins=admins
    )

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
