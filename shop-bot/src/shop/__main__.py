"""Точка входа.

    python -m shop run        боевой режим
    python -m shop seed       залить демо-каталог в пустую базу
    python -m shop catalog    показать витрину в консоли
    python -m shop orders     последние заказы
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from .config import Settings, load_settings
from .handlers import build_router
from .handlers.admin import admin_commands
from .models import DELIVERY_TITLES, STATUS_TITLES
from .notify import Notifier
from .pricing import fmt_money
from .seed import seed
from .storage import Storage

log = logging.getLogger("shop")


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
    notifier = Notifier(bot, settings.admins(), settings)

    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(storage, notifier, settings))

    with contextlib.suppress(Exception):
        # Меню команд — украшение; падать из-за него при старте не стоит.
        await bot.set_my_commands(admin_commands())

    if not await storage.list_products(only_available=False):
        log.warning("каталог пуст — добавьте товары или залейте демо: python -m shop seed")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    log.info("запускаюсь: %s", settings.shop_name)
    tasks = [
        asyncio.create_task(dispatcher.start_polling(bot, handle_signals=False), name="bot"),
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
        print("Залил демо-каталог. Посмотреть: python -m shop catalog")
        return 0
    print("В базе уже есть товары — ничего не менял.")
    return 0


async def command_catalog(settings: Settings) -> int:
    async with Storage(settings.db_path) as storage:
        categories = await storage.list_categories(only_active=False)
        products = await storage.list_products(only_available=False)
        if not products:
            print("Каталог пуст. Залейте демо: python -m shop seed")
            return 1

        titles = {item.id: item.title for item in categories}
        grouped: dict[str, list] = {}
        for product in products:
            grouped.setdefault(titles.get(product.category_id, "Без категории"), []).append(product)

        for category_title, items in grouped.items():
            print(f"\n{category_title}")
            for item in items:
                stock = "∞" if item.stock is None else f"{item.stock} шт."
                hidden = "" if item.active else "  (скрыт)"
                print(
                    f"  #{item.id:<3} {item.title:<34} "
                    f"{fmt_money(item.price, settings.currency):>10}  {stock}{hidden}"
                )
    return 0


async def command_orders(settings: Settings, args: argparse.Namespace) -> int:
    async with Storage(settings.db_path) as storage:
        orders = await storage.list_orders(limit=args.limit)
        if not orders:
            print("Заказов пока нет.")
            return 0

        for order in orders:
            when = order.created_at.strftime("%d.%m %H:%M") if order.created_at else ""
            print(
                f"\n№{order.id} · {when} · {STATUS_TITLES.get(order.status, order.status)} · "
                f"{fmt_money(order.total, settings.currency)}"
            )
            print(f"  {order.name} · {order.phone} · "
                  f"{DELIVERY_TITLES.get(order.delivery, order.delivery)}")
            if order.address:
                print(f"  {order.address}")
            for line in order.lines:
                print(f"    {line.title} ×{line.qty} = "
                      f"{fmt_money(line.total, settings.currency)}")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shop", description="Telegram-магазин с корзиной")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="боевой режим")
    sub.add_parser("seed", help="залить демо-каталог в пустую базу")
    sub.add_parser("catalog", help="показать каталог в консоли")

    orders = sub.add_parser("orders", help="последние заказы")
    orders.add_argument("--limit", type=int, default=10)

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
        if args.command == "catalog":
            return asyncio.run(command_catalog(settings))
        if args.command == "orders":
            return asyncio.run(command_orders(settings, args))
    except KeyboardInterrupt:
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
