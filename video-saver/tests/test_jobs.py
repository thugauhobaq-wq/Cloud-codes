"""Очередь: путь задачи от ссылки до готового файла и все повороты по дороге."""

from __future__ import annotations

import threading

import pytest
from conftest import FakeDownloader, wait_for, wait_status

from vsaver.jobs import Queue, Settings
from vsaver.limits import LimitError
from vsaver.store import AUDIO, CANCELLED, DONE, FAILED, QUEUED, RUNNING, VIDEO
from vsaver.urls import UrlError


class TestHappyPath:
    def test_job_reaches_done(self, running, store, downloader):
        job = running.submit("https://example.com/v", resolve=False)
        found = wait_status(store, job.id, DONE, FAILED)
        assert found.status == DONE
        assert found.percent == 100
        assert found.title == downloader.title
        assert found.ext == "mp4"
        assert found.total == downloader.size

    def test_file_lands_in_storage(self, running, store):
        job = running.submit("https://example.com/v", resolve=False)
        wait_status(store, job.id, DONE, FAILED)
        path = store.find_file(job.id)
        assert path is not None and path.stat().st_size > 0

    def test_audio_gets_mp3(self, running, store):
        job = running.submit("https://example.com/v", kind=AUDIO, resolve=False)
        found = wait_status(store, job.id, DONE, FAILED)
        assert found.status == DONE
        assert found.ext == "mp3"

    def test_url_is_normalised_before_saving(self, running, store):
        job = running.submit(
            "https://example.com/v?id=1&utm_source=tg&list=PL1", resolve=False
        )
        assert "utm_source" not in store.get(job.id).url
        assert "list=" not in store.get(job.id).url

    def test_progress_reaches_listeners(self, running, store):
        seen = []
        job = running.submit("https://example.com/v", resolve=False)
        running.listen(job.id, seen.append)
        wait_status(store, job.id, DONE, FAILED)
        wait_for(lambda: any(event.get("status") == DONE for event in seen))
        assert any(event.get("status") == DONE for event in seen)


class TestRejections:
    def test_bad_url_never_becomes_a_job(self, running, store):
        with pytest.raises(UrlError):
            running.submit("file:///etc/passwd")
        assert store.recent() == []

    def test_limit_error_never_becomes_a_job(self, store, downloader, limits):
        queue = Queue(store, Settings(workers=1, limits=limits), downloader)
        for _ in range(limits.per_hour):
            queue.guard.note_job("1.2.3.4")
        with pytest.raises(LimitError):
            queue.submit("https://example.com/v", ip="1.2.3.4", resolve=False)
        assert store.recent() == []

    def test_probe_failure_explains_itself(self, store, limits):
        downloader = FakeDownloader(probe_error="ролик приватный")
        queue = Queue(store, Settings(workers=1, limits=limits), downloader)
        queue.start()
        try:
            job = queue.submit("https://example.com/v", resolve=False)
            found = wait_status(store, job.id, DONE, FAILED)
            assert found.status == FAILED
            assert found.error == "ролик приватный"
            assert downloader.fetches == []  # качать даже не начинали
        finally:
            queue.stop(timeout=2)

    def test_fetch_failure_is_reported(self, store, limits):
        downloader = FakeDownloader(fetch_error="площадка закрыла доступ")
        queue = Queue(store, Settings(workers=1, limits=limits), downloader)
        queue.start()
        try:
            job = queue.submit("https://example.com/v", resolve=False)
            found = wait_status(store, job.id, DONE, FAILED)
            assert found.status == FAILED
            assert found.error == "площадка закрыла доступ"
        finally:
            queue.stop(timeout=2)

    def test_too_long_video_is_stopped_before_download(self, store, limits):
        downloader = FakeDownloader(duration=limits.max_duration + 60)
        queue = Queue(store, Settings(workers=1, limits=limits), downloader)
        queue.start()
        try:
            job = queue.submit("https://example.com/v", resolve=False)
            found = wait_status(store, job.id, DONE, FAILED)
            assert found.status == FAILED
            assert "длиннее" in found.error
            assert downloader.fetches == []
        finally:
            queue.stop(timeout=2)

    def test_too_big_promise_is_stopped_before_download(self, store, limits):
        downloader = FakeDownloader(size=limits.max_size * 2)
        queue = Queue(store, Settings(workers=1, limits=limits), downloader)
        queue.start()
        try:
            job = queue.submit("https://example.com/v", resolve=False)
            found = wait_status(store, job.id, DONE, FAILED)
            assert found.status == FAILED
            assert downloader.fetches == []
        finally:
            queue.stop(timeout=2)

    def test_lying_size_is_caught_mid_download(self, store, limits):
        """Площадка обещала мало, а отдаёт много — ловим по реальным байтам."""
        downloader = FakeDownloader(size=limits.max_size * 4, steps=8)
        # Опрос врёт про размер, поэтому первый рубеж пропускает задачу дальше.
        downloader.probe = lambda url, *, kind="video", quality="1080": _small_meta(downloader)
        queue = Queue(store, Settings(workers=1, limits=limits), downloader)
        queue.start()
        try:
            job = queue.submit("https://example.com/v", resolve=False)
            found = wait_status(store, job.id, DONE, FAILED, CANCELLED, timeout=10)
            assert found.status == FAILED
            assert "больше допустимого" in found.error
        finally:
            queue.stop(timeout=2)


def _small_meta(downloader):
    from vsaver.downloader import Meta

    return Meta(title=downloader.title, duration=10, size=1024, ext="mp4", site="test")


class TestCancel:
    def test_queued_job_cancels_at_once(self, store, downloader, limits):
        queue = Queue(store, Settings(workers=1, limits=limits), downloader)
        # Очередь не запущена — задача так и остаётся в ожидании.
        job = queue.submit("https://example.com/v", resolve=False)
        assert queue.cancel(job.id)
        assert store.get(job.id).status == CANCELLED

    def test_running_job_stops(self, store, limits):
        gate = threading.Event()
        downloader = FakeDownloader(steps=20, block=gate)
        queue = Queue(store, Settings(workers=1, limits=limits), downloader)
        queue.start()
        try:
            job = queue.submit("https://example.com/v", resolve=False)
            assert downloader.started.wait(timeout=5)
            queue.cancel(job.id)
            gate.set()  # отпускаем закачку — она должна увидеть отмену
            found = wait_status(store, job.id, CANCELLED, DONE, FAILED)
            assert found.status == CANCELLED
            assert store.find_file(job.id) is None
        finally:
            gate.set()
            queue.stop(timeout=2)

    def test_delete_removes_everything(self, running, store):
        job = running.submit("https://example.com/v", resolve=False)
        wait_status(store, job.id, DONE, FAILED)
        assert running.delete(job.id)
        assert store.get(job.id) is None
        assert store.find_file(job.id) is None

    def test_cancelling_finished_job_does_nothing(self, running, store):
        job = running.submit("https://example.com/v", resolve=False)
        wait_status(store, job.id, DONE, FAILED)
        assert not running.cancel(job.id)


class TestRestart:
    def test_interrupted_jobs_go_back_to_the_queue(self, store, downloader, limits):
        """Сервер перезапустили посреди закачки — задача должна доделаться."""
        job = store.create("https://example.com/v")
        store.update(job.id, status=RUNNING, percent=42, downloaded=999)

        queue = Queue(store, Settings(workers=1, limits=limits), downloader)
        queue.start()
        try:
            found = wait_status(store, job.id, DONE, FAILED)
            assert found.status == DONE
        finally:
            queue.stop(timeout=2)

    def test_queued_jobs_survive_restart(self, store, downloader, limits):
        job = store.create("https://example.com/v")
        assert store.get(job.id).status == QUEUED

        queue = Queue(store, Settings(workers=1, limits=limits), downloader)
        queue.start()
        try:
            assert wait_status(store, job.id, DONE, FAILED).status == DONE
        finally:
            queue.stop(timeout=2)


class TestWorkers:
    def test_several_downloads_run_at_once(self, store, limits):
        """Работников двое — значит, две закачки идут одновременно, а не по очереди."""
        gate = threading.Event()
        downloader = FakeDownloader(steps=6, block=gate)
        limits.per_ip_active = 5
        queue = Queue(store, Settings(workers=2, limits=limits), downloader)
        queue.start()
        try:
            first = queue.submit("https://example.com/1", ip="1.1.1.1", resolve=False)
            second = queue.submit("https://example.com/2", ip="1.1.1.1", resolve=False)
            running_both = wait_for(
                lambda: store.get(first.id).status == RUNNING
                and store.get(second.id).status == RUNNING
            )
            assert running_both
        finally:
            gate.set()
            queue.stop(timeout=3)

    def test_kind_and_quality_are_remembered(self, running, store):
        job = running.submit(
            "https://example.com/v", kind=VIDEO, quality="480", resolve=False
        )
        found = store.get(job.id)
        assert (found.kind, found.quality) == (VIDEO, "480")
