"""Командная строка.

    python -m botkit new shop --title "Магазин у дома"
    python -m botkit new booking --dir ~/projects --project booking-bot
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .scaffold import ScaffoldError, create_project, next_steps


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="botkit", description="Каркас Telegram-ботов")
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="создать новый проект бота из шаблона")
    new.add_argument("package", help="имя пакета: shop, booking, lead_magnet")
    new.add_argument("--dir", default=".", help="куда создать (по умолчанию текущий каталог)")
    new.add_argument("--project", help="имя каталога (по умолчанию <пакет>-bot)")
    new.add_argument("--title", help="название бота для README и приветствия")
    new.add_argument(
        "--force", action="store_true", help="перезаписать код в существующем каталоге"
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.command == "new":
        try:
            project = create_project(
                args.package,
                parent=Path(args.dir),
                directory=args.project,
                title=args.title,
                force=args.force,
            )
        except ScaffoldError as exc:
            # Пользовательская ошибка: трейсбек здесь только мешает.
            print(f"Не получилось: {exc}")
            return 1
        print(next_steps(project))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
