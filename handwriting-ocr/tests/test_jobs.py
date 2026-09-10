"""Очередь: успех, отказы движка, отмена, рестарт."""

from __future__ import annotations

import pytest
from conftest import JPEG, FakeRecognizer, wait_for

from hwocr.jobs import Queue, Settings
from hwocr.store import CANCELLED, DONE, FAILED, RUNNING, Store


@pytest.fixture()
def store(tmp_path):
    store = Store(tmp_path)
    yield store
    store.close()


def run_queue(store, recognizer):
    queue = Queue(store, Settings(keep_hours=1, keep_count=10), recognizer)
    queue.start()
    return queue


def upload(tmp_path, data=JPEG):
    path = tmp_path / "in.part"
    path.write_bytes(data)
    return path


def test_job_ends_with_text_and_engine_name(store, tmp_path):
    engine = FakeRecognizer("Здравствуй,\nмир")
    queue = run_queue(store, engine)
    try:
        job = queue.submit("лист.jpg", "image/jpeg", upload(tmp_path))
        done = wait_for(lambda: store.get(job.id).status == DONE and store.get(job.id))
    finally:
        queue.stop()
    assert done.text == "Здравствуй,\nмир"
    assert done.engine == "fake" and done.edited is False
    assert done.started and done.finished and done.finished >= done.started
    assert engine.calls == [(len(JPEG), "image/jpeg")]
    assert store.image_path(job.id).exists(), "фото остаётся до уборки — для превью"


def test_engine_refusal_becomes_a_readable_failure(store, tmp_path):
    queue = run_queue(store, FakeRecognizer(fail="ключ не подошёл"))
    try:
        job = queue.submit("a.jpg", "image/jpeg", upload(tmp_path))
        failed = wait_for(lambda: store.get(job.id).status == FAILED and store.get(job.id))
    finally:
        queue.stop()
    assert failed.error == "ключ не подошёл"
    assert failed.text == ""


def test_unexpected_crash_does_not_kill_the_worker(store, tmp_path):
    engine = FakeRecognizer(crash=True)
    queue = run_queue(store, engine)
    try:
        first = queue.submit("a.jpg", "image/jpeg", upload(tmp_path))
        wait_for(lambda: store.get(first.id).status == FAILED)
        assert "внутренняя ошибка" in store.get(first.id).error
        engine.crash = False
        second = queue.submit("b.jpg", "image/jpeg", upload(tmp_path))
        wait_for(lambda: store.get(second.id).status == DONE)
    finally:
        queue.stop()


def test_lost_photo_is_reported(store, tmp_path):
    queue = run_queue(store, FakeRecognizer())
    try:
        job = store.create("a.jpg", "image/jpeg", 1)
        queue._queue.put(job.id)
        failed = wait_for(lambda: store.get(job.id).status == FAILED and store.get(job.id))
    finally:
        queue.stop()
    assert "потеряно" in failed.error


def test_cancel_waiting_job(store):
    queue = Queue(store, recognizer=FakeRecognizer())
    job = store.create("a.jpg", "image/jpeg", 1)
    assert queue.cancel(job.id)
    assert store.get(job.id).status == CANCELLED
    assert not queue.cancel(job.id), "второй раз отменять нечего"


def test_cancel_during_recognition_discards_result(store, tmp_path):
    queue = run_queue(store, FakeRecognizer("не нужно", delay=0.3))
    try:
        job = queue.submit("a.jpg", "image/jpeg", upload(tmp_path))
        wait_for(lambda: store.get(job.id).status == RUNNING)
        assert queue.cancel(job.id)
        wait_for(lambda: store.get(job.id).status == CANCELLED)
    finally:
        queue.stop()
    assert store.get(job.id).text == ""


def test_restart_puts_interrupted_job_back(store, tmp_path):
    job = store.create("a.jpg", "image/jpeg", 1)
    store.image_path(job.id).write_bytes(JPEG)
    store.update(job.id, status=RUNNING)  # как будто сервер упал посреди работы
    queue = run_queue(store, FakeRecognizer("после рестарта"))
    try:
        wait_for(lambda: store.get(job.id).status == DONE)
    finally:
        queue.stop()
    assert store.get(job.id).text == "после рестарта"


def test_listeners_see_running_then_done(store, tmp_path):
    seen: list[dict] = []
    queue = run_queue(store, FakeRecognizer())
    try:
        job = store.create("a.jpg", "image/jpeg", 1)
        store.image_path(job.id).write_bytes(JPEG)
        queue.listen(job.id, seen.append)
        queue._queue.put(job.id)
        wait_for(lambda: seen and seen[-1]["status"] == DONE)
    finally:
        queue.stop()
    assert [item["status"] for item in seen] == [RUNNING, DONE]
    assert seen[-1]["text"] == "Привет, мир"


def test_start_without_engine_fails_loudly(store, monkeypatch):
    monkeypatch.delenv("YC_API_KEY", raising=False)
    monkeypatch.delenv("YC_FOLDER_ID", raising=False)
    monkeypatch.setenv("OCR_ENGINE", "yandex")
    queue = Queue(store)
    with pytest.raises(Exception, match="YC_API_KEY"):
        queue.start()
