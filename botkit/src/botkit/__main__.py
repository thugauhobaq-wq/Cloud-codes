"""Командная строка.

    python -m botkit new shop --title "Магазин у дома"
    python -m botkit new shop --push repo --public         новый репозиторий
    python -m botkit new shop --push branch                ветка в текущем
    python -m botkit publish shop-bot --push repo          опубликовать готовое
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .publish import MODE_BRANCH, MODE_REPO, MODES, Published, PublishError, publish
from .scaffold import Project, ScaffoldError, create_project, next_steps


def add_publish_options(parser: argparse.ArgumentParser) -> None:
    """Ключи публикации: одинаковы у `new` и у `publish`."""
    parser.add_argument(
        "--push",
        choices=MODES,
        help=f"опубликовать в GitHub: «{MODE_REPO}» — новый репозиторий, "
        f"«{MODE_BRANCH}» — ветка в текущем",
    )
    parser.add_argument("--repo", default="", help="имя репозитория (по умолчанию имя каталога)")
    parser.add_argument("--owner", default="", help="владелец или организация")
    parser.add_argument("--branch", default="", help="имя ветки")
    parser.add_argument("--public", action="store_true", help="публичный репозиторий")
    parser.add_argument("--message", default="", help="текст коммита")
    parser.add_argument("--token", default="", help="токен GitHub (по умолчанию GITHUB_TOKEN)")


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
    new.add_argument(
        "--standalone",
        action="store_true",
        help="положить копию botkit в vendor/ — проект собирается сам по себе "
        f"(включается само при --push {MODE_REPO})",
    )
    add_publish_options(new)

    ship = sub.add_parser("publish", help="отправить готовый проект в GitHub")
    ship.add_argument("directory", help="каталог проекта")
    add_publish_options(ship)

    return parser


def do_publish(directory: Path, args: argparse.Namespace, title: str) -> Published:
    """Опубликовать каталог. Токен — из ключа или из окружения."""
    return publish(
        directory,
        mode=args.push,
        token=args.token or os.environ.get("GITHUB_TOKEN", ""),
        name=args.repo,
        owner=args.owner or os.environ.get("GITHUB_OWNER", ""),
        private=not args.public,
        description=title,
        branch=args.branch,
        message=args.message,
    )


def command_new(args: argparse.Namespace) -> int:
    # Отдельный репозиторий обязан быть самодостаточным: botkit не в PyPI, и
    # без копии внутри такой репозиторий не собрался бы ни у кого.
    project: Project = create_project(
        args.package,
        parent=Path(args.dir),
        directory=args.project,
        title=args.title,
        force=args.force,
        standalone=args.standalone or args.push == MODE_REPO,
    )
    print(next_steps(project))

    if not args.push:
        return 0

    result = do_publish(project.directory, args, project.title)
    print(f"\nОпубликовано — {result.describe()}")
    return 0


def command_publish(args: argparse.Namespace) -> int:
    if not args.push:
        # Молча ничего не сделать хуже, чем сказать, чего не хватает.
        raise PublishError(f"укажите --push: {' или '.join(MODES)}")

    directory = Path(args.directory)
    result = do_publish(directory, args, f"Telegram-бот {directory.name}")
    print(f"Опубликовано — {result.describe()}")
    print(f"Файлов в коммите: {result.files}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    try:
        if args.command == "new":
            return command_new(args)
        if args.command == "publish":
            return command_publish(args)
    except (ScaffoldError, PublishError) as exc:
        # Пользовательская ошибка: трейсбек здесь только мешает.
        print(f"Не получилось: {exc}")
        return 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
