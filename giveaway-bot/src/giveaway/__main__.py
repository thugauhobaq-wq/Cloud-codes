"""Точка входа.

python -m giveaway run              боевой режим: бот и подведение итогов
python -m giveaway giveaways        список розыгрышей
python -m giveaway participants 3   кто участвует
python -m giveaway verify 3         пересчитать жребий и сверить с итогами
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from botkit import make_bot, run_bot, setup_logging

from .config import Settings, load_settings
from .draw import ticket
from .draw import verify as verify_draw
from .handlers import admin_commands, build_router
from .models import STATUS_TITLES
from .notify import Notify
from .storage import Storage
from .texts import fmt_deadline
from .workers import Deadlines


async def command_run(settings: Settings) -> None:
    settings.require()

    storage = Storage(settings.db_path)
    await storage.open()

    bot = make_bot(settings)
    notify = Notify(bot, settings.admins(), settings)

    await run_bot(
        bot=bot,
        routers=[build_router(storage, notify, settings)],
        workers=[Deadlines(storage, notify, settings)],
        commands=admin_commands(),
        storage=storage,
    )


async def command_giveaways(settings: Settings) -> int:
    async with Storage(settings.db_path) as storage:
        items = await storage.list_giveaways(limit=50)
        if not items:
            print("Розыгрышей нет.")
            return 0

        for item in items:
            status = STATUS_TITLES.get(item.status, item.status)
            print(f"\n#{item.id} {status} — {item.title}")
            print(f"  участников: {item.participants}, победителей: {item.winners_count}")
            if item.finished_at:
                print(f"  подведены: {fmt_deadline(item.finished_at)}")
            else:
                print(f"  итоги: {fmt_deadline(item.ends_at)}")
            if item.seed:
                print(f"  зерно: {item.seed}")
    return 0


async def command_participants(settings: Settings, args: argparse.Namespace) -> int:
    async with Storage(settings.db_path) as storage:
        giveaway = await storage.get_giveaway(args.id)
        if giveaway is None:
            print(f"Розыгрыша #{args.id} нет.")
            return 1

        print(f"«{giveaway.title}» — участников: {giveaway.participants}\n")
        for item in await storage.last_participants(args.id, limit=args.limit):
            who = f"@{item.username}" if item.username else (item.name or "без имени")
            print(f"  {item.tg_id:<12} {who}")
    return 0


async def command_verify(settings: Settings, args: argparse.Namespace) -> int:
    """Пересчитать жребий и сверить с объявленными победителями.

    То же самое может сделать любой участник — по зерну из объявления.
    """
    async with Storage(settings.db_path) as storage:
        giveaway = await storage.get_giveaway(args.id)
        if giveaway is None or not giveaway.seed:
            print(f"Розыгрыш #{args.id} не завершён — проверять нечего.")
            return 1

        participants = await storage.participant_ids(args.id)
        winners = await storage.winners(args.id)
        announced = [item.tg_id for item in winners]
        ok = verify_draw(participants, giveaway.winners_count, giveaway.seed, announced)

        print(f"Розыгрыш #{giveaway.id}: «{giveaway.title}»")
        print(f"  зерно:      {giveaway.seed}")
        print(f"  участников: {len(participants)}")
        print("  билеты (первые пять по возрастанию):")
        for tg_id in sorted(participants, key=lambda item: ticket(giveaway.seed, item))[:5]:
            mark = " ← победитель" if any(w.tg_id == tg_id for w in winners) else ""
            print(f"    {tg_id:<12} {ticket(giveaway.seed, tg_id)[:24]}…{mark}")
        print(f"\n  результат: {'совпадает' if ok else 'НЕ СОВПАДАЕТ'}")
    return 0 if ok else 1


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="giveaway", description="Бот розыгрышей")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="боевой режим")
    sub.add_parser("giveaways", help="список розыгрышей")

    participants = sub.add_parser("participants", help="кто участвует")
    participants.add_argument("id", type=int)
    participants.add_argument("--limit", type=int, default=20)

    verify = sub.add_parser("verify", help="пересчитать жребий и сверить с итогами")
    verify.add_argument("id", type=int)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    settings = load_settings()
    setup_logging(settings.log_level)

    try:
        if args.command == "run":
            asyncio.run(command_run(settings))
            return 0
        if args.command == "giveaways":
            return asyncio.run(command_giveaways(settings))
        if args.command == "participants":
            return asyncio.run(command_participants(settings, args))
        if args.command == "verify":
            return asyncio.run(command_verify(settings, args))
    except KeyboardInterrupt:
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
