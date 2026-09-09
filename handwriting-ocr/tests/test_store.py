"""Хранилище: записи, файлы и уборка."""

from __future__ import annotations

import time

from hwocr.store import DONE, FAILED, QUEUED, RUNNING, Store


def test_round_trip_keeps_every_field(tmp_path):
    store = Store(tmp_path)
    job = store.create("лист.jpg", "image/jpeg", 1234, engine="fake")
    saved = store.get(job.id)
    assert saved.name == "лист.jpg"
    assert saved.media_type == "image/jpeg"
    assert saved.engine == "fake"
    assert saved.status == QUEUED and saved.text == "" and saved.edited is False
    assert [item.id for item in store.recent()] == [job.id]
    store.close()


def test_set_text_marks_human_edits(tmp_path):
    store = Store(tmp_path)
    job = store.create("a.jpg", "image/jpeg", 1)
    store.set_text(job.id, "распознано", edited=False)
    assert store.get(job.id).edited is False
    store.set_text(job.id, "поправлено", edited=True)
    again = store.get(job.id)
    assert again.text == "поправлено" and again.edited is True
    assert again.as_dict()["edited"] is True


def test_delete_removes_the_photo_too(tmp_path):
    store = Store(tmp_path)
    job = store.create("a.jpg", "image/jpeg", 1)
    store.image_path(job.id).write_bytes(b"\xff\xd8\xff")
    assert store.delete(job.id)
    assert not store.image_path(job.id).exists()
    assert store.get(job.id) is None
    assert not store.delete(job.id)


def test_pending_returns_queued_and_interrupted(tmp_path):
    store = Store(tmp_path)
    waiting = store.create("a.jpg", "image/jpeg", 1)
    interrupted = store.create("b.jpg", "image/jpeg", 1)
    store.update(interrupted.id, status=RUNNING)
    finished = store.create("c.jpg", "image/jpeg", 1)
    store.update(finished.id, status=DONE)
    assert {item.id for item in store.pending()} == {waiting.id, interrupted.id}


def test_sweep_removes_old_results_with_their_photos(tmp_path):
    store = Store(tmp_path)
    paths = []
    for index in range(5):
        job = store.create(f"{index}.jpg", "image/jpeg", 1)
        store.image_path(job.id).write_bytes(b"x")
        paths.append(store.image_path(job.id))
        status = DONE if index % 2 else FAILED
        store.update(job.id, status=status, finished=time.time() - 3600 * 100)
    assert store.sweep(keep_hours=1, keep_count=2) == 3
    assert len(store.recent()) == 2
    assert sum(path.exists() for path in paths) == 2


def test_sweep_keeps_fresh_and_unfinished(tmp_path):
    store = Store(tmp_path)
    fresh = store.create("fresh.jpg", "image/jpeg", 1)
    store.update(fresh.id, status=DONE, finished=time.time())
    waiting = store.create("waiting.jpg", "image/jpeg", 1)
    assert store.sweep(keep_hours=1, keep_count=0) == 0
    assert {item.id for item in store.recent()} == {fresh.id, waiting.id}
