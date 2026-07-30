"""Командная строка: сайты, сервер, бот, импорт и модерация из консоли."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

from . import telegram
from .demo import generate
from .models import (
    DEFAULT_SETTINGS,
    Review,
    Site,
    ValidationError,
    normalize_domains,
    normalize_settings,
)
from .server import serve
from .storage import DEFAULT_DB, Storage


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m widget",
        description="Виджет отзывов для сайта: сервер, кабинет модерации и Telegram-бот.",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="путь к базе SQLite")
    sub = parser.add_subparsers(dest="command", required=True)

    site = sub.add_parser("site", help="управление сайтами-клиентами")
    site_sub = site.add_subparsers(dest="site_command", required=True)

    add = site_sub.add_parser("add", help="завести новый сайт и получить строчку кода")
    add.add_argument("--name", required=True, help="название компании")
    add.add_argument("--domains", default="", help="через запятую: example.ru, www.example.ru")
    add.add_argument("--demo", action="store_true", help="сразу набить демо-отзывами")

    site_sub.add_parser("list", help="все сайты")

    show = site_sub.add_parser("show", help="ключ, токен и строчка для вставки")
    show.add_argument("--site", required=True)

    setter = site_sub.add_parser("set", help="изменить настройки сайта")
    setter.add_argument("--site", required=True)
    setter.add_argument("--name")
    setter.add_argument("--domains")
    setter.add_argument("--auto-approve", choices=["on", "off"])
    setter.add_argument("--auto-approve-from", type=int, choices=[1, 2, 3, 4, 5])
    setter.add_argument("--telegram-token")
    setter.add_argument("--telegram-chat")
    setter.add_argument("--setting", action="append", default=[], metavar="КЛЮЧ=ЗНАЧЕНИЕ",
                        help=f"настройка виджета: {', '.join(sorted(DEFAULT_SETTINGS))}")

    remove = site_sub.add_parser("rm", help="удалить сайт вместе с отзывами")
    remove.add_argument("--site", required=True)

    server = sub.add_parser("serve", help="запустить сервер виджета")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8080)
    server.add_argument("--public-url", default="",
                        help="внешний адрес, если сервер стоит за прокси")

    bot = sub.add_parser("bot", help="Telegram-бот, собирающий отзывы")
    bot.add_argument("--site", help="ключ сайта; без него — все сайты с токеном")

    demo = sub.add_parser("demo", help="набить сайт демо-отзывами")
    demo.add_argument("--site", required=True)
    demo.add_argument("--count", type=int, default=18)
    demo.add_argument("--seed", type=int, default=None)

    imp = sub.add_parser("import", help="загрузить отзывы из JSON или CSV")
    imp.add_argument("--site", required=True)
    imp.add_argument("--file", required=True)
    imp.add_argument("--publish", action="store_true", help="сразу опубликовать")

    listing = sub.add_parser("reviews", help="показать отзывы в консоли")
    listing.add_argument("--site", required=True)
    listing.add_argument("--status", choices=["pending", "published", "rejected"])
    listing.add_argument("--limit", type=int, default=20)

    moderate = sub.add_parser("moderate", help="опубликовать или отклонить отзыв")
    moderate.add_argument("--id", type=int, required=True)
    moderate.add_argument("--action", choices=["publish", "reject", "pin", "unpin"],
                          required=True)

    export = sub.add_parser("export", help="выгрузить отзывы сайта в JSON")
    export.add_argument("--site", required=True)

    return parser


# ------------------------------------------------------------------ вывод

def embed_line(site: Site, base: str = "http://localhost:8080") -> str:
    return f'<script src="{base}/w/{site.key}.js" async></script>'


def print_site(site: Site, storage: Storage, with_token: bool = False) -> None:
    counts = storage.counts(site.key)
    print(f"{site.name}")
    print(f"  ключ:        {site.key}")
    if with_token:
        print(f"  токен:       {site.admin_token}")
    print(f"  домены:      {', '.join(site.domains) or 'любые'}")
    print(f"  автопубликация: {'да' if site.auto_approve else 'нет'}"
          f" (от {site.auto_approve_from}★)")
    print(f"  отзывы:      {counts['published']} опубликовано, {counts['pending']} на модерации")
    print(f"  вставка:     {embed_line(site)}")
    print(f"  форма:       http://localhost:8080/f/{site.key}")
    print(f"  админка:     http://localhost:8080/admin?site={site.key}")


# --------------------------------------------------------------- импорт

def read_rows(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json" or text.lstrip().startswith(("[", "{")):
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("reviews") or data.get("items") or []
        return [row for row in data if isinstance(row, dict)]
    sample = text[:2048]
    try:
        dialect: Any = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = "excel"
    return list(csv.DictReader(text.splitlines(), dialect=dialect))


FIELD_ALIASES = {
    "text": ("text", "отзыв", "текст", "comment", "review", "body", "message"),
    "rating": ("rating", "оценка", "звёзды", "звезды", "score", "stars", "mark"),
    "author": ("author", "имя", "name", "user", "автор", "клиент"),
    "contact": ("contact", "контакт", "email", "почта", "phone", "телефон"),
}


def pick(row: dict[str, Any], field: str) -> Any:
    lowered = {str(key).strip().lower(): value for key, value in row.items() if key}
    for alias in FIELD_ALIASES[field]:
        if alias in lowered:
            return lowered[alias]
    return ""


def do_import(storage: Storage, site: Site, path: Path, publish: bool) -> tuple[int, int]:
    added = skipped = 0
    for row in read_rows(path):
        try:
            review = Review.build(
                site=site.key,
                text=pick(row, "text"),
                rating=pick(row, "rating") or 5,
                author=pick(row, "author"),
                contact=pick(row, "contact"),
                source="import",
            )
        except ValidationError:
            skipped += 1
            continue
        if publish:
            review.status = "published"
        if storage.add_review(review)[1]:
            added += 1
        else:
            skipped += 1
    return added, skipped


# --------------------------------------------------------------- команды

def cmd_site(args: argparse.Namespace, storage: Storage) -> int:
    if args.site_command == "add":
        site = Site.create(args.name, domains=normalize_domains(args.domains))
        storage.add_site(site)
        if args.demo:
            storage.add_many(generate(site.key))
        print("Сайт создан.\n")
        print_site(site, storage, with_token=True)
        print("\nТокен показывается один раз — сохраните его, он нужен для входа в админку.")
        return 0

    if args.site_command == "list":
        sites = storage.list_sites()
        if not sites:
            print("Пока ни одного сайта. Заведите первый: python -m widget site add --name «...»")
            return 0
        for site in sites:
            print_site(site, storage)
            print()
        return 0

    site = storage.get_site(args.site)
    if site is None:
        print(f"Сайт {args.site} не найден", file=sys.stderr)
        return 1

    if args.site_command == "show":
        print_site(site, storage, with_token=True)
        return 0

    if args.site_command == "rm":
        storage.delete_site(site.key)
        print(f"Удалён сайт {site.key} и все его отзывы")
        return 0

    fields: dict[str, Any] = {}
    if args.name:
        fields["name"] = args.name
    if args.domains is not None and args.domains != "":
        fields["domains"] = normalize_domains(args.domains)
    if args.auto_approve:
        fields["auto_approve"] = args.auto_approve == "on"
    if args.auto_approve_from:
        fields["auto_approve_from"] = args.auto_approve_from
    if args.telegram_token is not None:
        fields["telegram_token"] = args.telegram_token
    if args.telegram_chat is not None:
        fields["telegram_chat"] = args.telegram_chat

    raw_settings: dict[str, Any] = {}
    for pair in args.setting:
        if "=" not in pair:
            print(f"Настройка «{pair}» без знака = — пропускаю", file=sys.stderr)
            continue
        key, value = pair.split("=", 1)
        raw_settings[key.strip()] = value.strip()
    if raw_settings:
        fields["settings"] = normalize_settings(raw_settings, site.settings)

    updated = storage.update_site(site.key, **fields)
    print_site(updated, storage)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    serve(host=args.host, port=args.port, db_path=args.db, public_url=args.public_url)
    return 0


def cmd_bot(args: argparse.Namespace, storage: Storage) -> int:
    if args.site:
        site = storage.get_site(args.site)
        if site is None:
            print(f"Сайт {args.site} не найден", file=sys.stderr)
            return 1
        keys = [site.key]
    else:
        keys = [site.key for site in storage.sites_with_bot()]

    if not keys:
        print("Нет ни одного сайта с токеном бота.\n"
              "Задайте его: python -m widget site set --site КЛЮЧ --telegram-token 123:AA...",
              file=sys.stderr)
        return 1
    storage.close()
    telegram.run_bots(args.db, keys)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "serve":
        return cmd_serve(args)

    with Storage(args.db) as storage:
        if args.command == "site":
            return cmd_site(args, storage)

        if args.command == "bot":
            return cmd_bot(args, storage)

        if args.command == "moderate":
            review = storage.get_review(args.id)
            if review is None:
                print(f"Отзыв {args.id} не найден", file=sys.stderr)
                return 1
            if args.action in ("publish", "reject"):
                storage.set_status(args.id, "published" if args.action == "publish" else "rejected")
            else:
                storage.set_pinned(args.id, args.action == "pin")
            print(f"Отзыв {args.id}: {args.action}")
            return 0

        site = storage.get_site(getattr(args, "site", ""))
        if site is None:
            print(f"Сайт {getattr(args, 'site', '')} не найден", file=sys.stderr)
            return 1

        if args.command == "demo":
            added = storage.add_many(generate(site.key, count=args.count, seed=args.seed))
            print(f"Добавлено демо-отзывов: {added}")
            print(f"Смотреть: http://localhost:8080/preview/{site.key}")
            return 0

        if args.command == "import":
            path = Path(args.file)
            if not path.is_file():
                print(f"Файл {path} не найден", file=sys.stderr)
                return 1
            added, skipped = do_import(storage, site, path, args.publish)
            print(f"Загружено: {added}, пропущено: {skipped}")
            return 0

        if args.command == "reviews":
            reviews = storage.list_reviews(site.key, status=args.status, limit=args.limit)
            if not reviews:
                print("Отзывов нет")
                return 0
            for review in reviews:
                mark = "★" * review.rating + "☆" * (5 - review.rating)
                flag = {"pending": "…", "published": "✓", "rejected": "✗"}[review.status]
                print(f"[{review.id:>4}] {flag} {mark}  {review.author} — {review.created_at[:10]}")
                print(f"       {review.text[:110]}")
                if review.reply:
                    print(f"       ↳ ответ: {review.reply[:90]}")
            return 0

        if args.command == "export":
            reviews = storage.list_reviews(site.key, limit=10000)
            print(json.dumps([review.admin() for review in reviews],
                             ensure_ascii=False, indent=2))
            return 0

    return 1
