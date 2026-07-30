from __future__ import annotations

from botkit import Broadcaster, Progress
from botkit.testing import FakeSender


def broadcaster(sender: FakeSender, **kwargs) -> Broadcaster:
    # rate побольше, чтобы тесты не ждали: проверяем логику, а не темп.
    kwargs.setdefault("rate", 1000)
    return Broadcaster(sender, **kwargs)


async def test_everyone_gets_the_message():
    sender = FakeSender()
    progress = await broadcaster(sender).run([1, 2, 3], "Привет")

    assert sender.sent == [(1, "Привет"), (2, "Привет"), (3, "Привет")]
    assert progress.sent == 3
    assert progress.processed == 3


async def test_blocked_recipients_are_reported():
    sender = FakeSender(blocked={2})
    marked: list[int] = []

    async def on_blocked(chat_id: int) -> None:
        marked.append(chat_id)

    progress = await broadcaster(sender, on_blocked=on_blocked).run([1, 2], "Привет")

    assert progress.sent == 1
    assert progress.blocked == 1
    assert marked == [2]


async def test_temporary_failures_do_not_mark_anyone():
    """Сетевой сбой — не повод вычёркивать человека из базы навсегда."""
    marked: list[int] = []

    async def on_blocked(chat_id: int) -> None:
        marked.append(chat_id)

    progress = await broadcaster(FakeSender(failing={1}), on_blocked=on_blocked).run(
        [1, 2], "Привет"
    )

    assert progress.failed == 1
    assert progress.sent == 1
    assert marked == []


async def test_empty_audience():
    progress = await broadcaster(FakeSender()).run([], "Никому")
    assert progress == Progress(total=0)


async def test_progress_is_reported_on_the_way_and_at_the_end():
    seen: list[int] = []

    async def on_progress(progress: Progress) -> None:
        seen.append(progress.sent)

    await broadcaster(FakeSender()).run(
        [1, 2, 3, 4], "Привет", on_progress=on_progress, progress_every=2
    )

    # Дважды по ходу (после 2 и 4) и один раз в конце.
    assert seen == [2, 4, 4]


async def test_rate_limit_paces_the_run():
    """Темп ниже лимита Telegram: превысишь — рассылку срежут на середине."""
    import time

    sender = FakeSender()
    started = time.perf_counter()
    await Broadcaster(sender, rate=50).run([1, 2, 3], "Привет")
    elapsed = time.perf_counter() - started

    # Две паузы по 1/50 секунды между тремя сообщениями.
    assert elapsed >= 0.03
    assert len(sender.sent) == 3
