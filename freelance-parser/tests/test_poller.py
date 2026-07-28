"""Цикл опроса: холодный старт, устойчивость к сбоям, дедупликация."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from fparser.models import Order
from fparser.poller import Poller
from fparser.sources import Fetcher
from fparser.sources.flru import FlRuSource
from fparser.sources.kwork import KworkSource
from fparser.storage import Filters, Storage

FIXTURES = Path(__file__).parent / "fixtures"


class FakeSender:
    """Подмена отправщика: тесты не должны ходить в Telegram."""

    def __init__(self, *, deliver: bool = True) -> None:
        self.orders: list[Order] = []
        self.texts: list[str] = []
        self.deliver = deliver

    async def send_orders(self, orders: list[Order]) -> list[Order]:
        if not self.deliver:
            return []
        self.orders.extend(orders)
        return list(orders)

    async def send_text(self, text: str, **_kwargs: object) -> bool:
        self.texts.append(text)
        return True


def build_poller(storage: Storage, sender: FakeSender | None = None, **kwargs) -> Poller:
    notifications: list[str] = []

    async def notify(text: str) -> bool:
        notifications.append(text)
        return True

    poller = Poller(
        sources=[KworkSource(), FlRuSource()],
        fetcher=Fetcher(max_attempts=1),
        storage=storage,
        sender=sender,  # type: ignore[arg-type]
        notify=notify,
        **kwargs,
    )
    poller.notifications = notifications  # type: ignore[attr-defined]
    return poller


def mock_both_boards() -> None:
    respx.get("https://kwork.ru/projects").mock(
        return_value=httpx.Response(200, content=(FIXTURES / "kwork_state.html").read_bytes())
    )
    respx.get("https://www.fl.ru/rss/all.xml").mock(
        return_value=httpx.Response(200, content=(FIXTURES / "flru.xml").read_bytes())
    )


@respx.mock
async def test_cold_start_sends_nothing(storage: Storage):
    """Первый запуск не должен вывалить в канал сотню заказов, которые давно
    разобрали без нас."""
    mock_both_boards()
    sender = FakeSender()
    poller = build_poller(storage, sender)

    await poller.process()

    assert sender.orders == []
    assert await storage.count_orders() == 6
    assert any("Инициализация" in text for text in poller.notifications)


@respx.mock
async def test_second_poll_sends_only_new(storage: Storage):
    mock_both_boards()
    sender = FakeSender()
    poller = build_poller(storage, sender)

    await poller.process()  # холодный старт
    await poller.process()  # ничего нового не появилось

    assert sender.orders == []

    respx.get("https://kwork.ru/projects").mock(
        return_value=httpx.Response(
            200,
            content=b'<script>window.stateData = {"wants":[{"id":777,"name":'
            b'"\xd0\x9d\xd0\xbe\xd0\xb2\xd1\x8b\xd0\xb9 \xd0\xb7\xd0\xb0\xd0\xba\xd0\xb0\xd0\xb7",'
            b'"description":"x","price":9000}]};</script>',
        )
    )

    await poller.process()

    assert [order.uid for order in sender.orders] == ["kwork:777"]


@respx.mock
async def test_filtered_orders_are_marked_seen(storage: Storage):
    """Отсеянные тоже помечаем просмотренными — иначе после смены фильтров они
    прилетят пачкой как «новые»."""
    mock_both_boards()
    await storage.save_filters(Filters(keywords=["несуществующее-слово"]))
    sender = FakeSender()
    poller = build_poller(storage, sender)

    await poller.process()  # холодный старт
    await poller.process()

    assert sender.orders == []
    assert await storage.count_orders() == 6


@respx.mock
async def test_undelivered_orders_are_retried(storage: Storage):
    """Если Telegram отказал, заказ не должен потеряться навсегда."""
    mock_both_boards()
    poller = build_poller(storage, FakeSender())
    await poller.process()  # холодный старт

    respx.get("https://kwork.ru/projects").mock(
        return_value=httpx.Response(
            200,
            content=b'<script>window.stateData = {"wants":[{"id":777,"name":"Order",'
            b'"description":"x","price":9000}]};</script>',
        )
    )

    failing = FakeSender(deliver=False)
    poller._sender = failing  # type: ignore[attr-defined]
    await poller.process()

    assert await storage.order_by_uid("kwork:777") is None

    working = FakeSender()
    poller._sender = working  # type: ignore[attr-defined]
    await poller.process()

    assert [order.uid for order in working.orders] == ["kwork:777"]


@respx.mock
async def test_one_broken_board_does_not_stop_the_rest(storage: Storage):
    respx.get("https://kwork.ru/projects").mock(return_value=httpx.Response(500))
    respx.get("https://www.fl.ru/rss/all.xml").mock(
        return_value=httpx.Response(200, content=(FIXTURES / "flru.xml").read_bytes())
    )
    poller = build_poller(storage, FakeSender())

    report = await poller.collect()

    by_slug = {item.slug: item for item in report.sources}
    assert by_slug["kwork"].error is not None
    assert by_slug["flru"].found == 3


@respx.mock
async def test_network_failure_does_not_raise(storage: Storage):
    respx.get("https://kwork.ru/projects").mock(side_effect=httpx.ConnectError("нет сети"))
    respx.get("https://www.fl.ru/rss/all.xml").mock(
        return_value=httpx.Response(200, content=(FIXTURES / "flru.xml").read_bytes())
    )
    poller = build_poller(storage, FakeSender())

    report = await poller.collect()

    assert {item.slug for item in report.sources} == {"kwork", "flru"}
    assert report.found_total == 3


@respx.mock
async def test_blocked_board_gets_a_penalty_and_one_warning(storage: Storage):
    """При 403 надо отступить, а не долбиться каждый цикл — и предупредить
    ровно один раз, иначе предупреждения сами станут спамом."""
    respx.get("https://kwork.ru/projects").mock(return_value=httpx.Response(403))
    respx.get("https://www.fl.ru/rss/all.xml").mock(
        return_value=httpx.Response(200, content=(FIXTURES / "flru.xml").read_bytes())
    )
    poller = build_poller(storage, FakeSender())

    await poller.collect()
    second = await poller.collect()

    kwork_report = next(item for item in second.sources if item.slug == "kwork")
    assert kwork_report.skipped is not None
    assert "блокировк" in kwork_report.skipped

    warnings = [text for text in poller.notifications if "403" in text]
    assert len(warnings) == 1


@respx.mock
async def test_disabled_board_is_not_requested(storage: Storage):
    mock_both_boards()
    await storage.save_filters(Filters(sources=["flru"]))
    poller = build_poller(storage, FakeSender())

    report = await poller.collect()

    kwork_report = next(item for item in report.sources if item.slug == "kwork")
    assert kwork_report.skipped == "отключена"
    # Выключенная площадка не должна даже опрашиваться — это лишний запрос,
    # который приближает нас к блокировке ни за что.
    assert all(call.request.url.host != "kwork.ru" for call in respx.calls)


@respx.mock
async def test_pause_stops_delivery(storage: Storage):
    mock_both_boards()
    await storage.save_filters(Filters(paused=True))
    sender = FakeSender()
    poller = build_poller(storage, sender)

    report = await poller.process()

    assert report.sources == []
    assert sender.orders == []


@respx.mock
async def test_stats_are_recorded(storage: Storage):
    mock_both_boards()
    poller = build_poller(storage, FakeSender())

    await poller.process()  # холодный старт статистику не пишет
    await poller.process()

    rows = await storage.stats_since(7)

    assert {row["source"] for row in rows} == {"kwork", "flru"}


@respx.mock
async def test_skip_dedup_shows_everything(storage: Storage):
    """`dryrun --all` должен показывать всю ленту, а не только незнакомое."""
    mock_both_boards()
    poller = build_poller(storage, FakeSender())
    await poller.process()

    report = await poller.collect(skip_dedup=True)

    assert len(report.candidates) == 6


@respx.mock
async def test_collect_can_target_one_board(storage: Storage):
    mock_both_boards()
    poller = build_poller(storage, FakeSender())

    report = await poller.collect(only="flru")

    assert [item.slug for item in report.sources] == ["flru"]


def test_jitter_keeps_delay_in_range(storage: Storage):
    poller = build_poller(storage, interval=180)

    delays = [poller.next_delay() for _ in range(50)]

    assert all(126 <= delay <= 234 for delay in delays)
    assert len(set(delays)) > 1


@pytest.mark.parametrize("status", [500, 502, 503])
@respx.mock
async def test_server_errors_are_reported_not_raised(storage: Storage, status: int):
    respx.get("https://kwork.ru/projects").mock(return_value=httpx.Response(status))
    respx.get("https://www.fl.ru/rss/all.xml").mock(return_value=httpx.Response(200, content=b""))
    poller = build_poller(storage, FakeSender())

    report = await poller.collect()

    assert next(item for item in report.sources if item.slug == "kwork").error is not None
