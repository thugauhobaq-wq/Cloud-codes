"""Командная строка: поднять сервер или распознать файл прямо здесь.

Приложение для телефона — основной способ, но проверить ключ и посмотреть,
что движок выдаёт на конкретном фото, проще из терминала: без сервера и базы.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .jobs import Settings
from .recognize import MEDIA_TYPES, RecognitionError, build_recognizer, sniff_image


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hwocr",
        description="Фото рукописи → печатный текст.",
    )
    commands = parser.add_subparsers(dest="command")

    serve = commands.add_parser("serve", help="поднять приложение для телефона")
    serve.add_argument("--host", default="0.0.0.0", help="адрес (по умолчанию все)")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--data", default="data", help="каталог с задачами и фото")
    serve.add_argument("--max-upload", type=int, default=15, help="предел загрузки, МБ")
    serve.add_argument(
        "--keep-hours", type=float, default=24.0, help="через сколько часов удалять результаты"
    )
    serve.add_argument(
        "--keep-count", type=int, default=200, help="сколько последних результатов хранить всегда"
    )

    recognize = commands.add_parser("recognize", help="распознать файл")
    recognize.add_argument("source", type=Path, help="фото: JPEG, PNG или WebP")
    recognize.add_argument("--engine", choices=("claude", "yandex"), help="вместо OCR_ENGINE")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    if args.command == "serve":
        return _serve(args)
    if args.command == "recognize":
        return _recognize(args)
    parser.print_help()
    return 2


def _serve(args: argparse.Namespace) -> int:
    # Настройки проверяются до старта: сервер без ключа не поднимается,
    # а пишет, чего не хватает, и выходит с ошибкой.
    try:
        recognizer = build_recognizer()
    except RecognitionError as error:
        print(error, file=sys.stderr)
        return 1

    from .server import serve

    serve(
        args.host,
        args.port,
        args.data,
        settings=Settings(keep_hours=args.keep_hours, keep_count=args.keep_count),
        max_upload=max(1, args.max_upload) * 1024 * 1024,
        recognizer=recognizer,
    )
    return 0


def _recognize(args: argparse.Namespace) -> int:
    if not args.source.is_file():
        print(f"нет файла {args.source}", file=sys.stderr)
        return 1
    image = args.source.read_bytes()
    media_type = sniff_image(image[:16])
    if media_type not in MEDIA_TYPES:
        print("это не JPEG, PNG или WebP", file=sys.stderr)
        return 1

    env = dict(os.environ)
    if args.engine:
        env["OCR_ENGINE"] = args.engine
    try:
        recognizer = build_recognizer(env)
        text = recognizer.recognize(image, media_type)
    except RecognitionError as error:
        print(error, file=sys.stderr)
        return 1
    print(text)
    return 0
