"""Точка входа.

    python -m leadmagnet run             боевой режим: бот + догоняющие
    python -m leadmagnet seed            залить демо-магниты
    python -m leadmagnet magnets         список материалов и ссылок
    python -m leadmagnet stats 30        воронка за период
    python -m leadmagnet export leads.csv
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import logging
import signal
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from .broadcast import Broadcaster, telegram_sender
from .config import Settings, load_settings
from .delivery import Deliverer
from .followups import FollowUps
from .handlers import build_router
from .handlers.admin import admin_commands
from .seed import seed
from .storage import Storage

log = logging.getLogger("leadmagnet")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def command_run(settings: Settings) -> None:
    settings.require_telegram()

    storage = Storage(settings.db_path)
    await storage.open()

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    sender = telegram_sender(bot)
    deliverer = Deliverer(bot, storage)
    broadcaster = Broadcaster(storage, settings, sender)
    followups = FollowUps(storage, settings, sender)

    async def notify_owner(text: str) -> None:
        with contextlib.suppress(Exception):
            await bot.send_message(settings.owner_id, text)

    dispatcher = Dispatcher()
    dispatcher.include_router(
        build_router(storage, deliverer, broadcaster, settings, notify_owner)
    )

    with contextlib.suppress(Exception):
        # Меню команд — украшение; падать из-за него при старте не стоит.
        await bot.set_my_commands(admin_commands())

    if not await storage.list_magnets():
        log.warning("магнитов нет — заведите их командой /magnet_add или python -m leadmagnet seed")
    if settings.check_subscription and not settings.channel_link():
        log.warning(
            "проверка подписки включена, но ссылки на канал нет: "
            "укажите CHANNEL_URL, иначе кнопка «Подписаться» не появится"
        )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    log.info("запускаюсь: %s", settings.project_name)
    tasks = [
        asyncio.create_task(dispatcher.start_polling(bot, handle_signals=False), name="bot"),
        asyncio.create_task(followups.run_forever(), name="followups"),
        asyncio.create_task(stop.wait(), name="stop"),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if task.get_name() != "stop":
                task.result()
    finally:
        log.info("останавливаюсь")
        await dispatcher.storage.close()
        await bot.session.close()
        await storage.close()


async def command_seed(settings: Settings) -> int:
    async with Storage(settings.db_path) as storage:
        filled = await seed(storage)
    if filled:
        print("Залил демо-магниты. Посмотреть: python -m leadmagnet magnets")
        return 0
    print("В базе уже есть магниты — ничего не менял.")
    return 0


async def command_magnets(settings: Settings) -> int:
    async with Storage(settings.db_path) as storage:
        magnets = await storage.list_magnets(only_active=False)
        if not magnets:
            print("Магнитов нет. Залейте демо: python -m leadmagnet seed")
            return 1

        for magnet in magnets:
            state = "включён" if magnet.active else "выключен"
            ready = "" if magnet.ready else "  ⚠️ нечего выдавать"
            print(f"\n{magnet.code} — {magnet.title} ({state}){ready}")
            print(f"  ссылка: {settings.start_link(magnet.code)}")
            if magnet.followup:
                print(f"  догоняющее через {settings.followup_hours} ч: {magnet.followup[:60]}…")
    return 0


async def command_stats(settings: Settings, args: argparse.Namespace) -> int:
    async with Storage(settings.db_path) as storage:
        stats = await storage.stats(args.days)
        print(f"Воронка за {args.days} дн.\n")
        print(f"  подписчиков всего:  {stats['total_leads']}")
        print(f"  пришло за период:   {stats['new_leads']}")
        print(f"  выдач материалов:   {stats['deliveries']}")
        print(f"  заблокировали бота: {stats['blocked']}")

        by_magnet = await storage.by_magnet(args.days)
        if by_magnet:
            print("\n  по материалам:")
            for code, title, count in by_magnet:
                print(f"    {code:<16} {title[:34]:<36} {count}")

        by_source = await storage.by_source(args.days)
        if by_source:
            print("\n  источники:")
            for label, count in by_source:
                print(f"    {label:<20} {count}")
    return 0


async def command_export(settings: Settings, args: argparse.Namespace) -> int:
    async with Storage(settings.db_path) as storage:
        rows = await storage.export_rows()

    if not rows:
        print("Выгружать пока нечего.")
        return 1

    path = Path(args.path)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(
            ["Telegram ID", "Имя", "Username", "Источник", "Пришёл", "Заблокировал", "Материалы"]
        )
        for tg_id, name, username, source, created_at, blocked, codes in rows:
            writer.writerow(
                [
                    tg_id,
                    name,
                    f"@{username}" if username else "",
                    source,
                    (created_at or "")[:19].replace("T", " "),
                    "да" if blocked else "",
                    codes,
                ]
            )
    print(f"Выгрузил {len(rows)} строк в {path}")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="leadmagnet", description="Бот выдачи лид-магнита и сбора базы"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="боевой режим: бот и догоняющие сообщения")
    sub.add_parser("seed", help="залить демо-магниты в пустую базу")
    sub.add_parser("magnets", help="показать материалы и ссылки")

    stats = sub.add_parser("stats", help="статистика воронки")
    stats.add_argument("days", nargs="?", type=int, default=30)

    export = sub.add_parser("export", help="выгрузить базу подписчиков в CSV")
    export.add_argument("path", nargs="?", default="leads.csv")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    settings = load_settings()
    setup_logging(settings.log_level)

    try:
        if args.command == "run":
            asyncio.run(command_run(settings))
            return 0
        if args.command == "seed":
            return asyncio.run(command_seed(settings))
        if args.command == "magnets":
            return asyncio.run(command_magnets(settings))
        if args.command == "stats":
            return asyncio.run(command_stats(settings, args))
        if args.command == "export":
            return asyncio.run(command_export(settings, args))
    except KeyboardInterrupt:
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
