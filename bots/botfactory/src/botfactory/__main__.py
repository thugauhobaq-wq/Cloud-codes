"""Точка входа.

    python -m botfactory run            боевой режим
    python -m botfactory builds         история сборок в консоли
    python -m botfactory make shop      собрать бота без Telegram
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from botkit import make_bot, run_bot, setup_logging

from .config import Settings, load_settings
from .factory import Factory, FactoryError
from .handlers import admin_commands, build_router
from .orders import OrderError, parse
from .storage import STATUS_TITLES, Storage


async def command_run(settings: Settings) -> None:
    settings.require()
    settings.check_publishing()

    storage = Storage(settings.db_path)
    await storage.open()

    async def on_startup() -> None:
        # Сборка, начатая перед падением процесса, не продолжится сама:
        # честнее показать её как неудачную, чем оставить «собирается».
        for build in await storage.unfinished():
            await storage.fail(build.id, "процесс перезапустился во время сборки")

    bot = make_bot(settings)
    await run_bot(
        bot=bot,
        routers=[build_router(storage, Factory(settings), settings)],
        commands=admin_commands(),
        storage=storage,
        on_startup=on_startup,
    )


async def command_builds(settings: Settings, args: argparse.Namespace) -> int:
    async with Storage(settings.db_path) as storage:
        items = await storage.recent(args.limit)
        if not items:
            print("Сборок не было.")
            return 0
        for build in items:
            when = build.created_at.strftime("%d.%m %H:%M") if build.created_at else ""
            print(f"\n№{build.id} · {when} · {STATUS_TITLES.get(build.status, build.status)}")
            print(f"  {build.title} ({build.package})")
            if build.url:
                print(f"  {build.url}")
            if build.error:
                print(f"  ошибка: {build.error}")
    return 0


async def command_make(settings: Settings, args: argparse.Namespace) -> int:
    """Тот же путь, что и из чата, — но из консоли.

    Полезно, когда бот ещё не поднят: проверить токен и права можно, не
    заводя Telegram-бота.
    """
    settings.check_publishing()
    try:
        order = parse(
            " ".join(args.words), mode=settings.publish_mode, private=settings.private_repos
        )
    except OrderError as exc:
        print(f"Не получилось: {exc}")
        return 1

    print(f"Собираю {order.package} — «{order.title}»…")
    try:
        result = await Factory(settings).build(order)
    except FactoryError as exc:
        print(f"Не получилось: {exc}")
        return 1

    print(f"Готово: {result.url}")
    print(f"Каталог: {result.directory}")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="botfactory", description="Фабрика Telegram-ботов")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="боевой режим")

    builds = sub.add_parser("builds", help="история сборок")
    builds.add_argument("--limit", type=int, default=10)

    make = sub.add_parser("make", help="собрать бота из консоли")
    make.add_argument("words", nargs="+", help="заказ словами: «бота для магазина»")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    settings = load_settings()
    setup_logging(settings.log_level)

    try:
        if args.command == "run":
            asyncio.run(command_run(settings))
            return 0
        if args.command == "builds":
            return asyncio.run(command_builds(settings, args))
        if args.command == "make":
            return asyncio.run(command_make(settings, args))
    except KeyboardInterrupt:
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
