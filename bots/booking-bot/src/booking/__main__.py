"""Точка входа.

    python -m booking run              боевой режим: бот + напоминания
    python -m booking seed             залить демо-услуги и расписание
    python -m booking slots            показать свободные слоты в консоли
    python -m booking agenda 3         расписание на ближайшие дни
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from datetime import timedelta

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from .config import Settings, load_settings
from .handlers import build_router
from .handlers.admin import admin_commands
from .notify import Notifier
from .reminders import Reminders
from .schedule import parse_date
from .seed import seed
from .service import BookingService
from .storage import Storage
from .texts import fmt_date, human_duration

log = logging.getLogger("booking")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


# ──────────────────────────────────────────────────────────────────────────────
# run
# ──────────────────────────────────────────────────────────────────────────────


async def command_run(settings: Settings) -> None:
    settings.require_telegram()

    storage = Storage(settings.db_path)
    await storage.open()

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    booking = BookingService(storage, settings)
    notifier = Notifier(bot, settings.admins(), settings.tz)
    reminders = Reminders(storage, booking, notifier, settings)

    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(storage, booking, notifier, settings))

    with contextlib.suppress(Exception):
        # Меню команд — украшение; падать из-за него при старте не стоит.
        await bot.set_my_commands(admin_commands())

    if not await storage.list_windows():
        log.warning("расписание не задано — записаться нельзя, задайте /hours пн-пт 10:00-20:00")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    log.info("запускаюсь: %s, часовой пояс %s", settings.business_name, settings.timezone)
    tasks = [
        asyncio.create_task(dispatcher.start_polling(bot, handle_signals=False), name="bot"),
        asyncio.create_task(reminders.run_forever(), name="reminders"),
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


# ──────────────────────────────────────────────────────────────────────────────
# офлайн-команды
# ──────────────────────────────────────────────────────────────────────────────


async def command_seed(settings: Settings) -> int:
    async with Storage(settings.db_path) as storage:
        filled = await seed(storage)
    if filled:
        print("Залил демо-услуги, мастеров и расписание. Посмотреть: python -m booking slots")
        return 0
    print("В базе уже есть услуги — ничего не менял.")
    return 0


async def command_slots(settings: Settings, args: argparse.Namespace) -> int:
    async with Storage(settings.db_path) as storage:
        booking = BookingService(storage, settings)
        services = await storage.list_services()
        if not services:
            print("Услуг нет. Залейте демо-данные: python -m booking seed")
            return 1

        if args.service:
            services = [item for item in services if item.id == args.service]
            if not services:
                print(f"Услуги #{args.service} нет.")
                return 1

        today = booking.today()
        day = parse_date(args.day, today) if args.day else today

        print(f"Свободное время на {fmt_date(day)} ({settings.timezone})\n")
        empty = True
        for service in services:
            slots = await booking.free_slots(day, service)
            times = ", ".join(slot.astimezone(settings.tz).strftime("%H:%M") for slot in slots)
            print(f"  {service.title} ({human_duration(service.duration_min)}):")
            print(f"    {times if times else '— нет свободных слотов'}")
            empty = empty and not slots

        if empty:
            print(
                "\nПусто. Проверьте рабочие часы (/hours), выходные (/dayoffs) "
                "и что день не в прошлом."
            )
        return 0


async def command_agenda(settings: Settings, args: argparse.Namespace) -> int:
    async with Storage(settings.db_path) as storage:
        booking = BookingService(storage, settings)
        today = booking.today()
        for shift in range(args.days):
            day = today + timedelta(days=shift)
            items = await booking.day_bookings(day)
            print(f"\n{fmt_date(day)} — записей: {len(items)}")
            for item in items:
                start = item.starts_at.astimezone(settings.tz).strftime("%H:%M")
                end = item.ends_at.astimezone(settings.tz).strftime("%H:%M")
                who = item.client_name or "без имени"
                phone = f" · {item.client_phone}" if item.client_phone else ""
                print(f"  {start}–{end}  {item.service_title} — {who}{phone}")
    return 0


# ──────────────────────────────────────────────────────────────────────────────


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="booking", description="Бот записи на услуги")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="боевой режим: бот и напоминания")
    sub.add_parser("seed", help="залить демо-услуги и расписание в пустую базу")

    slots = sub.add_parser("slots", help="показать свободные слоты, не запуская бота")
    slots.add_argument("--day", help="дата: 5.08, 2026-08-05, завтра (по умолчанию сегодня)")
    slots.add_argument("--service", type=int, help="только одна услуга, по её id")

    agenda = sub.add_parser("agenda", help="расписание записей на ближайшие дни")
    agenda.add_argument("days", nargs="?", type=int, default=3)

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
        if args.command == "slots":
            return asyncio.run(command_slots(settings, args))
        if args.command == "agenda":
            return asyncio.run(command_agenda(settings, args))
    except KeyboardInterrupt:
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
