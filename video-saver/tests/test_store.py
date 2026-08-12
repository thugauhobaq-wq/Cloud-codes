"""Хранилище задач: записи, файлы и уборка."""

from __future__ import annotations

import time

from vsaver.store import DONE, QUEUED, RUNNING, Store


class TestRecords:
    def test_create_and_get(self, store):
        job = store.create("https://example.com/v", site="example.com", ip="1.2.3.4")
        found = store.get(job.id)
        assert found is not None
        assert found.url == "https://example.com/v"
        assert found.status == QUEUED
        assert found.site == "example.com"

    def test_update(self, store):
        job = store.create("https://example.com/v")
        store.update(job.id, status=DONE, percent=100, title="Ролик")
        found = store.get(job.id)
        assert (found.status, found.percent, found.title) == (DONE, 100, "Ролик")

    def test_missing_job(self, store):
        assert store.get("0123456789abcdef") is None

    def test_ids_are_unique(self, store):
        made = {store.create("https://example.com/v").id for _ in range(50)}
        assert len(made) == 50


class TestPrivacy:
    def test_owner_and_ip_are_not_sent_to_the_browser(self, store):
        """Наружу уходит карточка задачи, а не то, кто и откуда её поставил."""
        job = store.create("https://example.com/v", owner="a" * 32, ip="1.2.3.4")
        payload = store.get(job.id).as_dict()
        assert "owner" not in payload
        assert "ip" not in payload
        assert "1.2.3.4" not in str(payload)

    def test_recent_filters_by_owner(self, store):
        mine = store.create("https://example.com/mine", owner="a" * 32)
        store.create("https://example.com/other", owner="b" * 32)
        found = store.recent(owner="a" * 32)
        assert [job.id for job in found] == [mine.id]

    def test_recent_without_owner_sees_everything(self, store):
        store.create("https://example.com/1", owner="a" * 32)
        store.create("https://example.com/2", owner="b" * 32)
        assert len(store.recent()) == 2


class TestCounters:
    def test_count_active(self, store):
        store.create("https://example.com/1", ip="1.1.1.1")
        second = store.create("https://example.com/2", ip="1.1.1.1")
        store.update(second.id, status=RUNNING)
        done = store.create("https://example.com/3", ip="1.1.1.1")
        store.update(done.id, status=DONE)

        assert store.count_active() == 2
        assert store.count_active(ip="1.1.1.1") == 2
        assert store.count_active(ip="2.2.2.2") == 0

    def test_count_since(self, store):
        old = store.create("https://example.com/1", ip="1.1.1.1")
        store.update(old.id, created=1000.0)
        store.create("https://example.com/2", ip="1.1.1.1")
        assert store.count_since("1.1.1.1", 2000.0) == 1


class TestFiles:
    def test_extension_is_cleaned(self, store):
        """Расширение подсказывает чужая сторона — в путь оно попадает чистым."""
        path = store.file_path("abc123", "../../etc/passwd")
        assert path.parent == store.files
        assert path.name.startswith("abc123")
        assert "/" not in path.name

    def test_find_file_without_knowing_extension(self, store):
        store.file_path("abc123", "mp4").write_bytes(b"data")
        found = store.find_file("abc123")
        assert found is not None and found.suffix == ".mp4"

    def test_delete_removes_file_too(self, store):
        job = store.create("https://example.com/v")
        path = store.file_path(job.id, "mp4")
        path.write_bytes(b"data")
        assert store.delete(job.id)
        assert not path.exists()
        assert store.get(job.id) is None

    def test_delete_removes_partials(self, store):
        """Недокачанные куски yt-dlp лежат рядом и тоже занимают место."""
        job = store.create("https://example.com/v")
        (store.files / f"{job.id}.mp4.part").write_bytes(b"half")
        store.delete(job.id)
        assert list(store.files.glob(f"{job.id}*")) == []

    def test_disk_used(self, store):
        (store.files / "a.mp4").write_bytes(b"\0" * 1000)
        (store.files / "b.mp4").write_bytes(b"\0" * 24)
        assert store.disk_used() == 1024


class TestSweep:
    def test_old_finished_jobs_are_removed(self, store):
        job = store.create("https://example.com/v")
        store.file_path(job.id, "mp4").write_bytes(b"data")
        store.update(job.id, status=DONE, finished=time.time() - 10 * 3600)
        assert store.sweep(keep_hours=4, keep_count=0) == 1
        assert store.get(job.id) is None

    def test_fresh_jobs_survive(self, store):
        job = store.create("https://example.com/v")
        store.update(job.id, status=DONE, finished=time.time())
        assert store.sweep(keep_hours=4, keep_count=0) == 0

    def test_running_jobs_survive(self, store):
        """Задача в работе не удаляется, даже если создана давно."""
        job = store.create("https://example.com/v")
        store.update(job.id, status=RUNNING, created=time.time() - 100 * 3600)
        assert store.sweep(keep_hours=1, keep_count=0) == 0
        assert store.get(job.id) is not None

    def test_keep_count_protects_last_jobs(self, store):
        for _ in range(5):
            job = store.create("https://example.com/v")
            store.update(job.id, status=DONE, finished=time.time() - 10 * 3600)
        store.sweep(keep_hours=1, keep_count=3)
        assert len(store.recent()) == 3

    def test_sweep_oldest_ignores_age(self, store):
        for number in range(3):
            job = store.create(f"https://example.com/{number}")
            store.update(job.id, status=DONE, finished=time.time())
        assert store.sweep_oldest(2) == 2
        assert len(store.recent()) == 1


class TestRestart:
    def test_pending_survives_reopen(self, tmp_path):
        """База лежит на диске: перезапуск сервера не теряет очередь."""
        first = Store(tmp_path / "data")
        job = first.create("https://example.com/v")
        first.update(job.id, status=RUNNING)
        first.close()

        second = Store(tmp_path / "data")
        try:
            assert [found.id for found in second.pending()] == [job.id]
        finally:
            second.close()
