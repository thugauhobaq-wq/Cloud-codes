"""Лимиты: то, чем открытый сервис заменяет пароль."""

from __future__ import annotations

import pytest

from vsaver.limits import Guard, LimitError, Limits, RateCounter, client_ip, plural


class TestPlural:
    """Человек видит эти сообщения в момент отказа — грамматика тут заметна."""

    @pytest.mark.parametrize(
        ("count", "expected"),
        [
            (1, "ролик"),
            (2, "ролика"),
            (4, "ролика"),
            (5, "роликов"),
            (11, "роликов"),  # одиннадцать, а не «один»
            (14, "роликов"),
            (21, "ролик"),
            (22, "ролика"),
            (25, "роликов"),
            (100, "роликов"),
            (101, "ролик"),
            (0, "роликов"),
        ],
    )
    def test_endings(self, count, expected):
        assert plural(count, "ролик", "ролика", "роликов") == expected


class TestRateCounter:
    def test_counts_within_window(self):
        counter = RateCounter(window=60)
        for _ in range(3):
            counter.add("1.2.3.4", now=1000.0)
        assert counter.count("1.2.3.4", now=1000.0) == 3

    def test_old_hits_fall_out(self):
        counter = RateCounter(window=60)
        counter.add("1.2.3.4", now=1000.0)
        counter.add("1.2.3.4", now=1030.0)
        # Через минуту с небольшим первое обращение уже не считается.
        assert counter.count("1.2.3.4", now=1065.0) == 1

    def test_addresses_are_independent(self):
        counter = RateCounter(window=60)
        counter.add("1.1.1.1", now=1000.0)
        assert counter.count("2.2.2.2", now=1000.0) == 0

    def test_wait_says_when_next_slot_opens(self):
        counter = RateCounter(window=60)
        counter.add("1.2.3.4", now=1000.0)
        counter.add("1.2.3.4", now=1010.0)
        # Потолок два: место освободится, когда выпадет самое раннее из двух.
        wait = counter.wait_for("1.2.3.4", 2, now=1020.0)
        assert 39 <= wait <= 42

    def test_no_wait_below_allowance(self):
        counter = RateCounter(window=60)
        counter.add("1.2.3.4", now=1000.0)
        assert counter.wait_for("1.2.3.4", 5, now=1000.0) == 0

    def test_forget_drops_empty_addresses(self):
        counter = RateCounter(window=60)
        counter.add("1.2.3.4", now=1000.0)
        counter.forget(now=2000.0)
        # Словарь не должен расти вечно: адрес без обращений забывается целиком.
        assert counter._hits == {}


class TestGuardQueue:
    def test_allows_first_job(self, store, limits):
        Guard(store, limits).check_new_job("1.2.3.4")

    def test_hourly_limit(self, store, limits):
        guard = Guard(store, limits)
        for _ in range(limits.per_hour):
            guard.note_job("1.2.3.4")
        with pytest.raises(LimitError) as error:
            guard.check_new_job("1.2.3.4")
        assert error.value.status == 429
        assert error.value.retry_after > 0

    def test_hourly_limit_is_per_address(self, store, limits):
        guard = Guard(store, limits)
        for _ in range(limits.per_hour):
            guard.note_job("1.2.3.4")
        guard.check_new_job("5.6.7.8")  # соседу это не мешает

    def test_active_jobs_per_address(self, store, limits):
        guard = Guard(store, limits)
        for _ in range(limits.per_ip_active):
            store.create("https://example.com/v", ip="1.2.3.4")
        with pytest.raises(LimitError, match="уже качается"):
            guard.check_new_job("1.2.3.4")

    def test_queue_length(self, store, limits):
        guard = Guard(store, limits)
        for number in range(limits.queue_length):
            store.create(f"https://example.com/{number}", ip=f"9.9.9.{number}")
        with pytest.raises(LimitError) as error:
            guard.check_new_job("1.2.3.4")
        assert error.value.status == 503

    def test_no_address_skips_personal_limits(self, store, limits):
        """Из терминала адреса нет — личные лимиты к такому запуску не применяются."""
        guard = Guard(store, limits)
        for _ in range(limits.per_hour * 2):
            guard.note_job("")
        guard.check_new_job("")


class TestGuardMedia:
    def test_long_video_rejected(self, store, limits):
        guard = Guard(store, limits)
        with pytest.raises(LimitError, match="длиннее") as error:
            guard.check_media(limits.max_duration + 1, 1024)
        assert error.value.status == 413

    def test_big_file_rejected(self, store, limits):
        guard = Guard(store, limits)
        with pytest.raises(LimitError, match="больше"):
            guard.check_media(60, limits.max_size + 1)

    def test_unknown_size_passes(self, store, limits):
        # Площадки часто не говорят размер заранее — это не повод отказывать.
        Guard(store, limits).check_media(0, 0)

    def test_exceeds_size_watches_real_bytes(self, store, limits):
        guard = Guard(store, limits)
        assert not guard.exceeds_size(limits.max_size)
        assert guard.exceeds_size(limits.max_size + 1)

    def test_zero_limit_means_no_limit(self, store):
        guard = Guard(store, Limits(max_size=0, max_duration=0))
        guard.check_media(10 ** 6, 10 ** 12)
        assert not guard.exceeds_size(10 ** 12)


class TestGuardDisk:
    def test_quota_blocks_new_jobs(self, store, limits):
        (store.files / "big.mp4").write_bytes(b"\0" * (limits.disk_quota + 1))
        with pytest.raises(LimitError) as error:
            Guard(store, limits).check_new_job("1.2.3.4")
        assert error.value.status == 507

    def test_free_space_removes_oldest(self, store, limits):
        """Место под новый файл освобождается сносом самых старых задач."""
        guard = Guard(store, limits)
        for number in range(4):
            job = store.create(f"https://example.com/{number}")
            store.update(job.id, status="done", finished=1000.0 + number)
            store.file_path(job.id, "mp4").write_bytes(b"\0" * (2 * 1024 * 1024))
        assert store.disk_used() == 8 * 1024 * 1024

        guard.free_space_for(4 * 1024 * 1024)
        assert store.disk_used() + 4 * 1024 * 1024 <= limits.disk_quota

    def test_free_space_keeps_running_jobs(self, store, limits):
        """Задачу, которая качается прямо сейчас, уборка трогать не должна."""
        guard = Guard(store, limits)
        job = store.create("https://example.com/v")
        store.update(job.id, status="running")
        store.file_path(job.id, "mp4").write_bytes(b"\0" * (9 * 1024 * 1024))
        guard.free_space_for(4 * 1024 * 1024)
        assert store.get(job.id) is not None


class TestClientIp:
    def test_socket_address_by_default(self):
        assert client_ip("203.0.113.9", "198.51.100.1", behind_proxy=False) == "203.0.113.9"

    def test_forwarded_header_when_allowed(self):
        assert client_ip("172.18.0.2", "198.51.100.1", behind_proxy=True) == "198.51.100.1"

    def test_first_address_of_the_chain(self):
        # Прокси дописывают адреса справа; исходный клиент — самый левый.
        forwarded = "198.51.100.1, 172.18.0.5, 172.18.0.2"
        assert client_ip("172.18.0.2", forwarded, behind_proxy=True) == "198.51.100.1"

    def test_forged_header_is_ignored_without_proxy(self):
        """Заголовок подделывается одной строкой в curl — без прокси ему не верим."""
        assert client_ip("203.0.113.9", "1.1.1.1", behind_proxy=False) == "203.0.113.9"

    def test_empty_header_falls_back(self):
        assert client_ip("203.0.113.9", "", behind_proxy=True) == "203.0.113.9"
