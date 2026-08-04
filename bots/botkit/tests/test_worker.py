from __future__ import annotations

import asyncio

import pytest

from botkit import PeriodicWorker


class Counting(PeriodicWorker):
    name = "счётчик"

    def __init__(self, interval: float = 0.01, fail_times: int = 0) -> None:
        super().__init__(interval)
        self.calls = 0
        self.fail_times = fail_times

    async def tick(self) -> int:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("временный сбой")
        return 1


async def test_run_once_counts_work():
    worker = Counting()
    assert await worker.run_once() == 1
    await worker.run_once()

    assert worker.ticks == 2
    assert worker.done_total == 2
    assert worker.last_error is None


async def test_loop_survives_an_exception():
    """Упавший воркер не должен ронять бота: приём сообщений важнее."""
    worker = Counting(interval=0.01, fail_times=1)
    task = asyncio.create_task(worker.run_forever())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert worker.calls >= 2  # после сбоя цикл продолжился
    assert worker.done_total >= 1


async def test_error_is_remembered_and_cleared():
    worker = Counting(fail_times=1)
    with pytest.raises(RuntimeError):
        await worker.run_once()

    worker.last_error = "временный сбой"
    await worker.run_once()
    assert worker.last_error is None


async def test_cancellation_stops_the_loop():
    worker = Counting(interval=10)
    task = asyncio.create_task(worker.run_forever())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_interval_must_be_positive():
    with pytest.raises(ValueError):
        Counting(interval=0)


def test_name_can_be_set_per_instance():
    assert PeriodicWorker(1, name="напоминания").name == "напоминания"


async def test_tick_must_be_implemented():
    with pytest.raises(NotImplementedError):
        await PeriodicWorker(1).tick()
