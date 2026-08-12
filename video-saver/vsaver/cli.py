"""Командная строка: поднять сайт, скачать ролик, присмотреть за очередью.

Веб-интерфейс — основной способ, но терминал нужен по двум причинам. Первая:
проверить, что вообще работает, не поднимая сервер. Вторая — присмотр. Сервис
открытый, и рано или поздно кто-то поставит в очередь то, чего не надо; веб-
админки для этого нет намеренно (меньше кода и нечего взламывать), а `jobs` и
`purge` по SSH решают ту же задачу.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .downloader import DEFAULT_QUALITY, QUALITIES, DownloadError, Progress, YtDlp
from .jobs import Settings
from .limits import Limits
from .store import AUDIO, VIDEO, Store
from .urls import UrlError
from .urls import check as check_url

GB = 1024 ** 3
MB = 1024 ** 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vsaver",
        description="Сайт для скачивания видео по ссылке.",
    )
    commands = parser.add_subparsers(dest="command")

    serve = commands.add_parser("serve", help="поднять сайт")
    serve.add_argument("--host", default="0.0.0.0", help="адрес (по умолчанию все)")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--data", default="data", help="каталог с задачами и файлами")
    serve.add_argument("--workers", type=int, default=2, help="одновременных закачек")
    serve.add_argument(
        "--behind-proxy",
        action="store_true",
        help="доверять X-Forwarded-For: включайте, только если впереди свой прокси",
    )
    _limit_arguments(serve)

    get = commands.add_parser("get", help="скачать ролик прямо здесь")
    get.add_argument("url", help="ссылка на видео")
    get.add_argument("--out", type=Path, default=Path("."), help="куда сохранить")
    get.add_argument("--audio", action="store_true", help="только звук, mp3")
    get.add_argument(
        "--quality", default=DEFAULT_QUALITY, choices=sorted(QUALITIES), help="высота кадра"
    )
    get.add_argument("--quiet", action="store_true", help="без прогресса")

    jobs = commands.add_parser("jobs", help="что в очереди на сервере")
    jobs.add_argument("--data", default="data")
    jobs.add_argument("--limit", type=int, default=30)

    purge = commands.add_parser("purge", help="почистить очередь и файлы")
    purge.add_argument("--data", default="data")
    purge.add_argument("--id", default="", help="удалить одну задачу по номеру")
    purge.add_argument("--all", action="store_true", help="удалить всё")
    purge.add_argument(
        "--older-than", type=float, default=0.0, help="удалить завершённые старше N часов"
    )
    return parser


def _limit_arguments(command: argparse.ArgumentParser) -> None:
    """Потолки сервиса. Значения по умолчанию — из `Limits`, чтобы не разъезжались."""
    base = Limits()
    command.add_argument(
        "--per-hour", type=int, default=base.per_hour, help="задач в час с одного адреса"
    )
    command.add_argument(
        "--per-ip-active",
        type=int,
        default=base.per_ip_active,
        help="одновременных задач с одного адреса",
    )
    command.add_argument(
        "--queue-length", type=int, default=base.queue_length, help="общая длина очереди"
    )
    command.add_argument(
        "--max-size", type=float, default=base.max_size / GB, help="предел размера файла, ГБ"
    )
    command.add_argument(
        "--max-duration",
        type=float,
        default=base.max_duration / 60,
        help="предел длительности ролика, минуты",
    )
    command.add_argument(
        "--disk-quota",
        type=float,
        default=base.disk_quota / GB,
        help="сколько занимать на диске, ГБ",
    )
    command.add_argument(
        "--keep-hours", type=float, default=base.keep_hours, help="сколько часов хранить файлы"
    )
    command.add_argument(
        "--keep-count", type=int, default=base.keep_count, help="сколько задач хранить всегда"
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    if args.command == "serve":
        return _serve(args)
    if args.command == "get":
        return _get(args)
    if args.command == "jobs":
        return _jobs(args)
    if args.command == "purge":
        return _purge(args)
    parser.print_help()
    return 2


def limits_of(args: argparse.Namespace) -> Limits:
    return Limits(
        per_hour=max(1, args.per_hour),
        per_ip_active=max(1, args.per_ip_active),
        queue_length=max(1, args.queue_length),
        max_size=int(max(0.0, args.max_size) * GB),
        max_duration=int(max(0.0, args.max_duration) * 60),
        disk_quota=int(max(0.0, args.disk_quota) * GB),
        keep_hours=max(0.0, args.keep_hours),
        keep_count=max(1, args.keep_count),
    )


def _serve(args: argparse.Namespace) -> int:
    from .server import serve

    limits = limits_of(args)
    print(
        f"лимиты: {limits.per_hour} задач в час с адреса, "
        f"до {limits.max_size / GB:.1f} ГБ и {limits.max_duration // 60} мин на ролик, "
        f"хранение {limits.keep_hours:g} ч"
    )
    serve(
        args.host,
        args.port,
        args.data,
        settings=Settings(workers=max(1, args.workers), limits=limits),
        behind_proxy=args.behind_proxy,
    )
    return 0


def _get(args: argparse.Namespace) -> int:
    try:
        target = check_url(args.url)
    except UrlError as error:
        print(error, file=sys.stderr)
        return 1

    kind = AUDIO if args.audio else VIDEO
    directory = args.out if args.out.is_dir() else args.out.parent
    directory.mkdir(parents=True, exist_ok=True)
    stem = directory / f"vsaver-{int(time.time())}"

    last = [""]

    def report(progress: Progress) -> None:
        if args.quiet:
            return
        line = f"  {progress.percent:3d}%  {progress.stage:9}"
        if progress.total:
            line += f" {progress.downloaded / MB:.0f}/{progress.total / MB:.0f} МБ"
        if line != last[0]:
            last[0] = line
            print(line, end="\r", file=sys.stderr, flush=True)

    downloader = YtDlp()
    try:
        meta = downloader.probe(target.url, kind=kind, quality=args.quality)
        if not args.quiet:
            print(f"{meta.title} — {meta.duration // 60} мин", file=sys.stderr)
        result = downloader.fetch(
            target.url,
            stem,
            kind=kind,
            quality=args.quality,
            report=report,
            should_stop=lambda: False,
        )
    except DownloadError as error:
        print(f"\n{error}", file=sys.stderr)
        return 1

    # Переименовываем во что-то читаемое: до скачивания название неизвестно.
    final = directory / _safe_name(f"{result.title}.{result.ext}")
    if final != result.path:
        result.path.replace(final)
    if not args.quiet:
        print(" " * 60, end="\r", file=sys.stderr)
    print(f"{final} — {result.size / MB:.1f} МБ")
    return 0


def _jobs(args: argparse.Namespace) -> int:
    store = Store(args.data)
    try:
        rows = store.recent(max(1, args.limit))
        if not rows:
            print("очередь пуста")
            return 0
        print(f"{'номер':<18}{'статус':<11}{'%':>4}  {'размер':>9}  адрес и название")
        for job in rows:
            size = f"{job.total / MB:.0f} МБ" if job.total else ""
            title = job.title or job.url
            print(
                f"{job.id:<18}{job.status:<11}{job.percent:>4}  {size:>9}  "
                f"{job.ip or '—'}  {title[:60]}"
            )
        used = store.disk_used()
        print(f"\nзанято на диске: {used / GB:.2f} ГБ")
    finally:
        store.close()
    return 0


def _purge(args: argparse.Namespace) -> int:
    store = Store(args.data)
    try:
        if args.id:
            print("удалено" if store.delete(args.id) else "такой задачи нет")
            return 0
        if args.all:
            removed = 0
            for job in store.recent(100000):
                if store.delete(job.id):
                    removed += 1
            print(f"удалено задач: {removed}")
            return 0
        if args.older_than > 0:
            print(f"удалено задач: {store.sweep(args.older_than, 0)}")
            return 0
        active = store.count_active()
        print(
            "укажите, что чистить: --id НОМЕР, --older-than ЧАСЫ или --all\n"
            f"сейчас в работе: {active}, занято {store.disk_used() / GB:.2f} ГБ"
        )
        return 2
    finally:
        store.close()


def _safe_name(name: str) -> str:
    """Имя файла, с которым справится любая файловая система."""
    cleaned = "".join(" " if ch in '\\/:*?"<>|' else ch for ch in name)
    cleaned = " ".join(cleaned.split()).strip(". ")
    return cleaned[:120] or "видео"


__all__ = ["build_parser", "limits_of", "main"]
