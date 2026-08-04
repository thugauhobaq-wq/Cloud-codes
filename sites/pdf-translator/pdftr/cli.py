"""Командная строка: поднять сервер или перевести файл прямо здесь.

Приложение для телефона — основной способ, но переводить из терминала тоже надо:
проверить настройки, прогнать пачку файлов, посмотреть, что вообще нашлось в
документе. Всё это без сервера и без базы.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .jobs import Settings
from .pdf.embed import FontError
from .pdf.reader import EncryptedPDFError, PDFError
from .pipeline import Progress, inspect, translate_document
from .translate.cache import Cache
from .translate.google import GoogleTranslator, TranslationError
from .translate.languages import TARGETS
from .translate.service import Service


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdftr",
        description="Перевод больших PDF с сохранением вёрстки.",
    )
    commands = parser.add_subparsers(dest="command")

    serve = commands.add_parser("serve", help="поднять приложение для телефона")
    serve.add_argument("--host", default="0.0.0.0", help="адрес (по умолчанию все)")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--data", default="data", help="каталог с задачами и кэшем")
    serve.add_argument("--workers", type=int, default=4, help="потоков перевода")
    serve.add_argument("--rate", type=float, default=4.0, help="запросов в секунду")
    serve.add_argument("--font", default="", help="путь к .ttf для печати перевода")
    serve.add_argument(
        "--max-upload", type=int, default=300, help="предел загрузки, МБ"
    )

    translate = commands.add_parser("translate", help="перевести файл")
    translate.add_argument("source", type=Path, help="исходный PDF")
    translate.add_argument("output", type=Path, nargs="?", help="куда сохранить")
    translate.add_argument("--from", dest="sl", default="auto", help="язык оригинала")
    translate.add_argument("--to", dest="tl", default="ru", help="язык перевода")
    translate.add_argument("--font", default="", help="путь к .ttf для печати перевода")
    translate.add_argument("--cache", default="", help="файл кэша переводов")
    translate.add_argument("--workers", type=int, default=4)
    translate.add_argument("--rate", type=float, default=4.0)
    translate.add_argument("--quiet", action="store_true", help="без прогресса")

    look = commands.add_parser("inspect", help="что внутри файла")
    look.add_argument("source", type=Path)

    commands.add_parser("languages", help="список языков")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    if args.command == "serve":
        return _serve(args)
    if args.command == "translate":
        return _translate(args)
    if args.command == "inspect":
        return _inspect(args)
    if args.command == "languages":
        for code, name in TARGETS.items():
            print(f"{code:8} {name}")
        return 0
    parser.print_help()
    return 2


def _serve(args: argparse.Namespace) -> int:
    from .server import serve

    serve(
        args.host,
        args.port,
        args.data,
        settings=Settings(workers=args.workers, rate=args.rate, font=args.font),
        max_upload=max(1, args.max_upload) * 1024 * 1024,
    )
    return 0


def _translate(args: argparse.Namespace) -> int:
    if not args.source.is_file():
        print(f"нет файла {args.source}", file=sys.stderr)
        return 1
    output = args.output or args.source.with_name(
        f"{args.source.stem} ({args.tl}){args.source.suffix}"
    )

    started = time.monotonic()
    last = [""]

    def report(progress: Progress) -> None:
        if args.quiet:
            return
        line = (
            f"  {progress.percent:3d}%  {progress.stage:10} "
            f"{progress.pages_done}/{progress.pages_total}"
        )
        if line != last[0]:
            last[0] = line
            print(line, end="\r", file=sys.stderr, flush=True)

    cache = Cache(args.cache or None)
    engine = GoogleTranslator(rate=args.rate)
    service = Service(cache, engine, workers=args.workers)
    family = None
    if args.font:
        from .pdf.typeface import family_for

        family = family_for(args.tl, args.font)

    try:
        result = translate_document(
            args.source.read_bytes(),
            source=args.sl,
            target=args.tl,
            cache=cache,
            service=service,
            family=family,
            report=report,
            title=args.source.stem,
        )
    except EncryptedPDFError as error:
        print(f"\n{error}", file=sys.stderr)
        return 1
    except (PDFError, FontError, TranslationError) as error:
        print(f"\nне получилось: {error}", file=sys.stderr)
        return 1
    finally:
        cache.close()

    output.write_bytes(result.data)
    if not args.quiet:
        print(" " * 60, end="\r", file=sys.stderr)
    spent = time.monotonic() - started
    print(
        f"{output}: {result.pages} стр., {result.blocks} абзацев, "
        f"{result.characters} знаков, {spent:.1f} с"
    )
    for warning in result.warnings:
        print(f"внимание: {warning}", file=sys.stderr)
    return 0


def _inspect(args: argparse.Namespace) -> int:
    if not args.source.is_file():
        print(f"нет файла {args.source}", file=sys.stderr)
        return 1
    try:
        found = inspect(args.source.read_bytes())
    except EncryptedPDFError as error:
        print(error, file=sys.stderr)
        return 1
    except PDFError as error:
        print(f"не разбирается: {error}", file=sys.stderr)
        return 1
    print(f"страниц: {found['pages']}")
    print(f"знаков в первых страницах: {found['sample_characters']}")
    if not found["has_text"]:
        print(
            "текстового слоя не нашлось: похоже, это скан. Прогоните его через "
            "распознавание — переводить картинку нечем."
        )
    return 0
