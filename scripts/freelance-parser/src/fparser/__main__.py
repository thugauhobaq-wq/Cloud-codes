"""Точка входа.

    python -m fparser run                 боевой режим: бот + опрос
    python -m fparser dryrun              один проход без отправки
    python -m fparser dryrun --offline    то же, но на сохранённых фикстурах
    python -m fparser probe kwork         сохранить живую страницу в фикстуру
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import re
import signal
import sys
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from .bot import build_router, setup_commands
from .config import Settings, load_settings
from .formatter import format_order
from .poller import Poller
from .sender import Sender
from .sources import ALL_SLUGS, Fetcher, build_sources
from .storage import Storage

log = logging.getLogger("fparser")

def _fixture_dir() -> Path:
    """Где лежат фикстуры.

    В рабочей копии — рядом с исходниками, а в установленном пакете такого
    каталога нет, и `probe` иначе пытался бы писать внутрь site-packages.
    """
    packaged = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
    if packaged.is_dir():
        return packaged
    return Path.cwd() / "fixtures"


FIXTURE_DIR = _fixture_dir()
FIXTURE_FILES = {
    "kwork": "kwork_state.html",
    "flru": "flru.xml",
    "habr": "habr.xml",
    "weblancer": "weblancer.xml",
}


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Штатные «Update id=... is handled» на каждый апдейт только зашумляют лог.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


class FixtureFetcher(Fetcher):
    """Подменяет сеть сохранёнными файлами.

    Нужен, чтобы `dryrun --offline` и тесты гонялись там, где до бирж не
    достучаться, — например, в CI или в песочнице с закрытым доступом.
    """

    def __init__(self, mapping: dict[str, Path]) -> None:
        super().__init__(client=httpx.AsyncClient())
        self._mapping = mapping

    async def get(self, url: str, **kwargs) -> httpx.Response:
        path = self._mapping.get(url)
        if path is None:
            raise FileNotFoundError(f"нет фикстуры для {url}")
        return httpx.Response(200, content=path.read_bytes())


def strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


# ──────────────────────────────────────────────────────────────────────────────
# run
# ──────────────────────────────────────────────────────────────────────────────


async def command_run(settings: Settings) -> None:
    settings.require_telegram()

    storage = Storage(settings.db_path)
    await storage.open()

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    fetcher = Fetcher(timeout=settings.request_timeout, proxy_url=settings.http_proxy_url)
    sender = Sender(bot, settings.target_chat_id)

    async def notify_owner(text: str) -> bool:
        return await sender.send_text(text, chat_id=settings.owner_id)

    poller = Poller(
        sources=build_sources(settings.feed_overrides()),
        fetcher=fetcher,
        storage=storage,
        sender=sender,
        interval=settings.poll_interval,
        notify=notify_owner,
    )

    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(storage, owner_id=settings.owner_id, poller=poller))

    await setup_commands(bot)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Без этого `docker stop` убивает процесс на полуслове: соединение с
        # БД остаётся незакрытым, а часть заказов — неотмеченной.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    log.info("запускаюсь: площадки %s", ", ".join(ALL_SLUGS))
    tasks = [
        asyncio.create_task(dispatcher.start_polling(bot, handle_signals=False), name="bot"),
        asyncio.create_task(poller.run_forever(), name="poller"),
        asyncio.create_task(stop.wait(), name="stop"),
    ]

    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        # Если поллер или бот упали с исключением, оно всплывёт здесь.
        for task in done:
            if task.get_name() != "stop":
                task.result()
    finally:
        log.info("останавливаюсь")
        await dispatcher.storage.close()
        await fetcher.aclose()
        await bot.session.close()
        await storage.close()


# ──────────────────────────────────────────────────────────────────────────────
# dryrun
# ──────────────────────────────────────────────────────────────────────────────


async def command_dryrun(settings: Settings, args: argparse.Namespace) -> int:
    storage = Storage(settings.db_path)
    await storage.open()

    sources = build_sources(settings.feed_overrides())

    if args.offline:
        missing = [
            slug for slug in FIXTURE_FILES if not (FIXTURE_DIR / FIXTURE_FILES[slug]).exists()
        ]
        if missing:
            print(f"Нет фикстур для: {', '.join(missing)} (искал в {FIXTURE_DIR})")
            await storage.close()
            return 1
        fetcher: Fetcher = FixtureFetcher(
            {source.feed_url: FIXTURE_DIR / FIXTURE_FILES[source.slug] for source in sources}
        )
        print(f"Режим без сети: беру данные из {FIXTURE_DIR}\n")
    else:
        fetcher = Fetcher(timeout=settings.request_timeout, proxy_url=settings.http_proxy_url)

    poller = Poller(
        sources=sources,
        fetcher=fetcher,
        storage=storage,
        interval=settings.poll_interval,
    )

    try:
        report = await poller.collect(only=args.source, skip_dedup=args.all)
    finally:
        await fetcher.aclose()

    print("Площадки:")
    for source_report in report.sources:
        if source_report.skipped:
            print(f"  {source_report.slug:<10} пропущена ({source_report.skipped})")
        elif source_report.error:
            print(f"  {source_report.slug:<10} ⚠️  {source_report.error}")
        else:
            print(
                f"  {source_report.slug:<10} найдено {source_report.found:>3}, "
                f"новых {source_report.fresh:>3}, подошло {source_report.accepted:>3}"
            )

    accepted = report.accepted
    print(f"\n{'─' * 60}\nПодошли ({len(accepted)}):\n")
    for order in accepted:
        print(strip_tags(format_order(order)))
        print(f"  {order.url}")
        print()

    rejected = report.rejected
    if rejected:
        print(f"{'─' * 60}\nОтсеяны ({len(rejected)}), первые 15:\n")
        for candidate in rejected[:15]:
            print(f"  ✗ {candidate.order.title[:60]:<60} — {candidate.result.reason}")

    await storage.close()

    if not report.sources or all(item.error for item in report.sources if not item.skipped):
        print("\nНи одна площадка не ответила — проверь сеть, прокси и адреса лент в .env")
        return 1
    return 0


# ──────────────────────────────────────────────────────────────────────────────
# probe
# ──────────────────────────────────────────────────────────────────────────────


async def command_probe(settings: Settings, args: argparse.Namespace) -> int:
    sources = {source.slug: source for source in build_sources(settings.feed_overrides())}
    source = sources.get(args.source)
    if source is None:
        print(f"Не знаю площадку {args.source}. Доступны: {', '.join(sources)}")
        return 1

    fetcher = Fetcher(timeout=settings.request_timeout, proxy_url=settings.http_proxy_url)
    try:
        response = await fetcher.get(source.feed_url)
    except Exception as exc:
        # probe — диагностический режим: пользователю нужна причина, а не трейсбек.
        print(f"Не удалось скачать {source.feed_url}: {exc}")
        return 1
    finally:
        await fetcher.aclose()

    suffix = ".html" if "html" in response.headers.get("content-type", "") else ".xml"
    out_path = Path(args.out) if args.out else FIXTURE_DIR / f"{source.slug}_probe{suffix}"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(response.content)

    print(f"Сохранил {len(response.content)} байт в {out_path}")

    try:
        orders = await source.fetch(FixtureFetcher({source.feed_url: out_path}))
    except Exception as exc:
        print(f"Разобрать не удалось: {exc}")
        return 1

    print(f"Разобрано заказов: {len(orders)}")
    for order in orders[:5]:
        print(f"  • {order.title[:70]} — {order.url}")
    if not orders:
        print(
            "\nЗаказов ноль — скорее всего, изменилась разметка. Фикстура сохранена, "
            "по ней можно поправить парсер."
        )
    return 0


# ──────────────────────────────────────────────────────────────────────────────


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fparser",
        description="Парсер заказов с фриланс-бирж в Telegram",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="боевой режим: бот + постоянный опрос")

    dryrun = sub.add_parser("dryrun", help="один проход без отправки в Telegram")
    dryrun.add_argument("--source", choices=ALL_SLUGS, help="опросить только одну площадку")
    dryrun.add_argument(
        "--all",
        action="store_true",
        help="показать всю ленту, а не только то, чего ещё не видели",
    )
    dryrun.add_argument(
        "--offline",
        action="store_true",
        help="работать на сохранённых фикстурах, без обращения к биржам",
    )

    probe = sub.add_parser("probe", help="сохранить живой ответ площадки в фикстуру")
    probe.add_argument("source", choices=ALL_SLUGS)
    probe.add_argument("--out", help="куда сохранить (по умолчанию tests/fixtures/)")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    settings = load_settings()
    setup_logging(settings.log_level)

    try:
        if args.command == "run":
            asyncio.run(command_run(settings))
            return 0
        if args.command == "dryrun":
            return asyncio.run(command_dryrun(settings, args))
        if args.command == "probe":
            return asyncio.run(command_probe(settings, args))
    except KeyboardInterrupt:
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
