"""Цикл опроса площадок.

Отвечает за то, чтобы опрос не останавливался: падение одной биржи не должно
влиять на остальные, а блокировка — приводить к бесконечному долблению в
закрытую дверь.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import filters as filters_module
from .models import Order
from .sender import Sender
from .sources import Fetcher, FetchError, Source
from .storage import Storage

log = logging.getLogger(__name__)

#: Разброс интервала. Опрос ровно по расписанию — заметный признак робота,
#: да и попадание всех площадок в одну секунду ни к чему.
JITTER = 0.3

#: Потолок штрафной паузы для заблокировавшей нас площадки.
MAX_PENALTY = 30 * 60

NotifyFn = Callable[[str], Awaitable[bool]]


@dataclass(slots=True)
class Candidate:
    order: Order
    result: filters_module.MatchResult


@dataclass(slots=True)
class SourceReport:
    slug: str
    found: int = 0
    fresh: int = 0
    accepted: int = 0
    error: str | None = None
    skipped: str | None = None


@dataclass(slots=True)
class PollReport:
    sources: list[SourceReport] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def accepted(self) -> list[Order]:
        return [item.order for item in self.candidates if item.result.passed]

    @property
    def rejected(self) -> list[Candidate]:
        return [item for item in self.candidates if not item.result.passed]

    @property
    def found_total(self) -> int:
        return sum(report.found for report in self.sources)


class Poller:
    def __init__(
        self,
        *,
        sources: list[Source],
        fetcher: Fetcher,
        storage: Storage,
        sender: Sender | None = None,
        interval: int = 180,
        notify: NotifyFn | None = None,
    ) -> None:
        self._sources = sources
        self._fetcher = fetcher
        self._storage = storage
        self._sender = sender
        self._interval = interval
        self._notify = notify

        self._penalty_until: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self._blocked_reported: set[str] = set()
        self._last_prune = 0.0
        # Держим ссылки на фоновые уведомления: без этого сборщик мусора может
        # утилизировать задачу до того, как она успеет отправиться.
        self._background: set[asyncio.Task[object]] = set()

        self.last_poll_at: datetime | None = None
        self.last_error: str | None = None

    # ── сбор ──────────────────────────────────────────────────────────────

    async def collect(self, *, only: str | None = None, skip_dedup: bool = False) -> PollReport:
        """Опросить площадки и отобрать заказы. Ничего не отправляет и не пишет.

        Разделение с `process` нужно ради `dryrun`: посмотреть, что нашлось,
        не разослав это в канал и не пометив просмотренным.

        `skip_dedup` показывает всю ленту, а не только незнакомое, — нужно,
        когда настраиваешь фильтры на уже работающем сервисе и хочешь понять,
        что вообще проходит, а не что нового появилось за последнюю минуту.
        """
        active_filters = await self._storage.get_filters()
        report = PollReport()

        for source in self._sources:
            if only and source.slug != only:
                continue
            if not only and source.slug not in active_filters.sources:
                report.sources.append(SourceReport(source.slug, skipped="отключена"))
                continue

            wait_left = self._penalty_until.get(source.slug, 0.0) - time.monotonic()
            if wait_left > 0:
                report.sources.append(
                    SourceReport(source.slug, skipped=f"пауза после блокировки, {int(wait_left)} с")
                )
                continue

            source_report = SourceReport(source.slug)
            try:
                orders = await source.fetch(self._fetcher)
            except FetchError as exc:
                source_report.error = str(exc)
                self._register_failure(source.slug, exc)
                log.warning("%s: %s", source.slug, exc)
            except Exception as exc:
                # Ловим всё: разметка на биржах меняется без предупреждения, и
                # свежий баг в одном парсере не должен лишать нас трёх остальных.
                source_report.error = f"ошибка разбора: {exc}"
                log.exception("%s: неожиданная ошибка", source.slug)
            else:
                self._register_success(source.slug)
                source_report.found = len(orders)

                await self._storage.note_categories(orders)
                fresh = orders if skip_dedup else await self._storage.select_new(orders)
                source_report.fresh = len(fresh)

                for order in fresh:
                    result = filters_module.match(order, active_filters)
                    report.candidates.append(Candidate(order, result))
                    if result.passed:
                        source_report.accepted += 1

            report.sources.append(source_report)

        self.last_poll_at = datetime.now(UTC)
        return report

    # ── полный проход ─────────────────────────────────────────────────────

    async def process(self) -> PollReport:
        """Опрос + отправка + фиксация состояния."""
        active_filters = await self._storage.get_filters()
        if active_filters.paused:
            log.debug("опрос на паузе")
            return PollReport()

        report = await self.collect()

        if await self._handle_cold_start(report):
            return report

        accepted = report.accepted
        delivered: list[Order] = []
        if accepted and self._sender is not None:
            delivered = await self._sender.send_orders(accepted)
        elif self._sender is None:
            delivered = accepted

        # Порядок важен: mark_seen вставляет строки через INSERT OR IGNORE,
        # поэтому доставленные помечаем первыми — иначе их перекроет запись
        # с sent=0 и в `/last` не попадёт ничего.
        await self._storage.mark_seen(delivered, sent=True)

        # Отсеянные фильтром тоже помечаем просмотренными, иначе они будут
        # заново прогоняться каждый опрос, а после смены настроек прилетят
        # пачкой как «новые». А вот принятые, но не доставленные (Telegram
        # ответил ошибкой), намеренно не трогаем — попробуем в следующий раз.
        undelivered = {order.uid for order in accepted} - {order.uid for order in delivered}
        await self._storage.mark_seen(
            (
                item.order
                for item in report.candidates
                if not item.result.passed and item.order.uid not in undelivered
            ),
            sent=False,
        )

        for source_report in report.sources:
            if source_report.error is None and source_report.skipped is None:
                await self._storage.record_stats(
                    source_report.slug,
                    found=source_report.found,
                    sent=source_report.accepted,
                )

        await self._storage.set_state("last_poll_at", datetime.now(UTC).isoformat())
        log.info(
            "опрос завершён: найдено %d, новых %d, отправлено %d",
            report.found_total,
            len(report.candidates),
            len(delivered),
        )
        await self._maybe_prune()
        return report

    async def _handle_cold_start(self, report: PollReport) -> bool:
        """Первый запуск: пометить всё просмотренным и промолчать.

        Иначе включение сервиса заканчивается сотней сообщений про заказы,
        которые давно разобрали без нас.
        """
        if await self._storage.get_state("initialized"):
            return False

        orders = [item.order for item in report.candidates]
        await self._storage.mark_seen(orders, sent=False)
        await self._storage.set_state("initialized", datetime.now(UTC).isoformat())

        log.info("холодный старт: %d заказов помечены просмотренными", len(orders))
        if self._notify is not None:
            await self._notify(
                f"✅ Инициализация завершена: {len(orders)} текущих заказов помечены "
                "просмотренными и не будут высланы.\n"
                "Дальше в канал пойдут только новые."
            )
        return True

    # ── реакция на блокировки ─────────────────────────────────────────────

    def _register_failure(self, slug: str, error: FetchError) -> None:
        if not error.is_blocked:
            return

        self._failures[slug] = self._failures.get(slug, 0) + 1
        penalty = min(60 * 2 ** (self._failures[slug] - 1), MAX_PENALTY)
        self._penalty_until[slug] = time.monotonic() + penalty
        log.warning("%s: блокировка, пауза %d с", slug, penalty)

        # Уведомляем один раз за серию: сообщение на каждый цикл превратило бы
        # предупреждение в тот же спам, от которого мы защищаемся.
        if slug not in self._blocked_reported and self._notify is not None:
            self._blocked_reported.add(slug)
            self._spawn(
                self._notify(
                    f"⚠️ {slug}: площадка отвечает {error.status_code}. "
                    f"Делаю паузу {penalty // 60} мин и пробую снова.\n"
                    "Если повторяется — помогает HTTP_PROXY_URL в .env."
                )
            )

    def _register_success(self, slug: str) -> None:
        if slug in self._blocked_reported and self._notify is not None:
            self._spawn(self._notify(f"✅ {slug}: доступ восстановлен."))
        self._failures.pop(slug, None)
        self._penalty_until.pop(slug, None)
        self._blocked_reported.discard(slug)

    def _spawn(self, coro: Awaitable[object]) -> None:
        """Запустить уведомление в фоне, не задерживая опрос."""
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _maybe_prune(self) -> None:
        if time.monotonic() - self._last_prune < 24 * 3600:
            return
        self._last_prune = time.monotonic()
        removed = await self._storage.prune()
        if removed:
            log.info("из истории удалено %d старых заказов", removed)

    # ── бесконечный цикл ──────────────────────────────────────────────────

    def next_delay(self) -> float:
        return self._interval * random.uniform(1 - JITTER, 1 + JITTER)

    async def run_forever(self) -> None:
        log.info("поллер запущен, интервал ~%d с", self._interval)
        while True:
            try:
                await self.process()
                self.last_error = None
            except asyncio.CancelledError:
                log.info("поллер остановлен")
                raise
            except Exception as exc:
                # Цикл обязан пережить любую ошибку: сервис, замолчавший из-за
                # единственного сбоя, бесполезен — а заметить это можно нескоро.
                self.last_error = str(exc)
                log.exception("сбой в цикле опроса")

            await asyncio.sleep(self.next_delay())
