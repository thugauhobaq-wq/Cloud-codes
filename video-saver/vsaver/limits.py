"""Лимиты: то, чем открытый сервис заменяет пароль.

Пароля здесь нет намеренно — сайт открыт всем. Но открытая качалка без потолков
живёт ровно до первого человека, который поставит в цикл скачивание сериала в
4K: диск кончится за вечер, трафик — за сутки, а счёт придёт владельцу VPS.

Ограничения разложены по трём рубежам, потому что дороже всего узнать о проблеме
поздно:

* до очереди — частота с адреса и длина очереди, это чистая арифметика;
* после опроса ссылки, но до закачки — длительность и обещанный размер: `probe`
  стоит секунду, а сэкономить может гигабайт;
* во время закачки — реальный размер, потому что обещанный бывает враньём, и
  свободное место на диске.

Счётчики частоты живут в памяти и обнуляются при перезапуске. Это осознанно:
хранить их негде, кроме той же базы, а перезапуск сервиса — событие настолько
редкое, что городить ради него что-то сложнее нет смысла.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

HOUR = 3600.0


class LimitError(Exception):
    """Лимит сработал. Текст показывается человеку, код уходит в HTTP-ответ."""

    def __init__(self, message: str, status: int = 429, retry_after: int = 0) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@dataclass(slots=True)
class Limits:
    """Потолки сервиса. Меняются параметрами запуска и видны в `/api/config`."""

    per_hour: int = 10
    """Сколько задач один адрес может создать за час."""

    per_ip_active: int = 2
    """Сколько задач одного адреса может выполняться и ждать одновременно."""

    queue_length: int = 40
    """Общая длина очереди: дальше сервис честно говорит, что занят."""

    max_size: int = 2 * 1024 * 1024 * 1024
    """Потолок размера файла, байты."""

    max_duration: int = 3 * 3600
    """Потолок длительности ролика, секунды."""

    disk_quota: int = 40 * 1024 * 1024 * 1024
    """Сколько места на диске сервис готов занять под скачанное, байты."""

    keep_hours: float = 4.0
    """Через сколько часов готовый файл удаляется."""

    keep_count: int = 200
    """Столько последних задач переживают уборку в любом случае."""

    def as_dict(self) -> dict:
        return {
            "perHour": self.per_hour,
            "perIpActive": self.per_ip_active,
            "queueLength": self.queue_length,
            "maxSize": self.max_size,
            "maxDuration": self.max_duration,
            "keepHours": self.keep_hours,
        }


class RateCounter:
    """Скользящее окно на час: сколько задач пришло с каждого адреса.

    Хранится только время создания задач, и только за последний час — словарь не
    растёт бесконечно, потому что пустые очереди выбрасываются при каждой чистке.
    """

    def __init__(self, window: float = HOUR) -> None:
        self.window = window
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def count(self, key: str, *, now: float | None = None) -> int:
        moment = time.time() if now is None else now
        with self._lock:
            return self._trim(key, moment)

    def add(self, key: str, *, now: float | None = None) -> None:
        moment = time.time() if now is None else now
        with self._lock:
            self._trim(key, moment)
            self._hits[key].append(moment)

    def wait_for(self, key: str, allowance: int, *, now: float | None = None) -> int:
        """Через сколько секунд у адреса освободится место под новую задачу."""
        moment = time.time() if now is None else now
        with self._lock:
            self._trim(key, moment)
            hits = self._hits.get(key)
            if not hits or len(hits) < allowance:
                return 0
            # Место освободится, когда из окна выпадет самая ранняя из тех
            # задач, что упираются в потолок.
            edge = hits[len(hits) - allowance]
            return max(1, int(edge + self.window - moment) + 1)

    def forget(self, *, now: float | None = None) -> None:
        """Выбросить адреса, о которых нечего помнить."""
        moment = time.time() if now is None else now
        with self._lock:
            for key in list(self._hits):
                if not self._trim(key, moment):
                    self._hits.pop(key, None)

    def _trim(self, key: str, now: float) -> int:
        hits = self._hits.get(key)
        if hits is None:
            return 0
        edge = now - self.window
        while hits and hits[0] < edge:
            hits.popleft()
        return len(hits)


class Guard:
    """Один вход для всех проверок: сервер спрашивает, можно ли, и получает ответ."""

    def __init__(self, store, limits: Limits | None = None) -> None:
        self.store = store
        self.limits = limits or Limits()
        self.rate = RateCounter()
        self._forgotten = 0.0

    # --- до очереди --------------------------------------------------------

    def check_new_job(self, ip: str) -> None:
        """Можно ли этому адресу поставить ещё одну задачу."""
        limits = self.limits
        self._housekeeping()

        if self.store.count_active() >= limits.queue_length:
            raise LimitError(
                "сервис сейчас занят — слишком много закачек в очереди. "
                "Попробуйте через минуту.",
                status=503,
                retry_after=60,
            )

        if ip:
            active = self.store.count_active(ip=ip)
            if active >= limits.per_ip_active:
                raise LimitError(
                    f"у вас уже качается {active} — дождитесь, пожалуйста, "
                    "и поставьте следующее.",
                    retry_after=30,
                )
            if self.rate.count(ip) >= limits.per_hour:
                wait = self.rate.wait_for(ip, limits.per_hour)
                roliki = plural(limits.per_hour, "ролик", "ролика", "роликов")
                raise LimitError(
                    f"с одного адреса можно скачивать {limits.per_hour} {roliki} в час. "
                    f"Следующий — через {_human_time(wait)}.",
                    retry_after=wait,
                )

        if self.store.disk_used() >= limits.disk_quota:
            # Место могло освободиться, просто уборка ещё не дошла.
            self.store.sweep(0, limits.keep_count)
            if self.store.disk_used() >= limits.disk_quota:
                raise LimitError(
                    "на сервере кончилось место под скачанное. "
                    "Попробуйте через несколько минут.",
                    status=507,
                    retry_after=300,
                )

    def note_job(self, ip: str) -> None:
        """Задача создана — засчитать её адресу."""
        if ip:
            self.rate.add(ip)

    # --- после опроса ссылки, до закачки ------------------------------------

    def check_media(self, duration: int, size: int) -> None:
        limits = self.limits
        if limits.max_duration and duration and duration > limits.max_duration:
            raise LimitError(
                f"ролик длиннее {_human_time(limits.max_duration)} — "
                "такие сервис не берёт",
                status=413,
            )
        if limits.max_size and size and size > limits.max_size:
            raise LimitError(
                f"файл больше {_human_size(limits.max_size)} — столько сервис не качает",
                status=413,
            )

    # --- во время закачки ---------------------------------------------------

    def exceeds_size(self, downloaded: int) -> bool:
        """Проверка на каждом кусочке: обещанный размер бывает враньём."""
        return bool(self.limits.max_size) and downloaded > self.limits.max_size

    def free_space_for(self, size: int) -> None:
        """Освободить место под ожидаемый файл, снося самое старое."""
        quota = self.limits.disk_quota
        if not quota:
            return
        for _ in range(20):
            if self.store.disk_used() + max(0, size) <= quota:
                return
            if not self.store.sweep_oldest(3):
                return

    def _housekeeping(self) -> None:
        """Раз в пять минут — уборка старых файлов и забытых счётчиков."""
        now = time.time()
        if now - self._forgotten < 300:
            return
        self._forgotten = now
        self.rate.forget(now=now)
        self.store.sweep(self.limits.keep_hours, self.limits.keep_count)


def client_ip(address: str, forwarded: str, *, behind_proxy: bool) -> str:
    """Настоящий адрес клиента.

    За обратным прокси реальный адрес приезжает в `X-Forwarded-For`, а в сокете
    сидит сам прокси — считать частоту по нему бессмысленно, все окажутся одним
    человеком. Но заголовок подделывается одной строкой в curl, поэтому верим ему
    только когда администратор явно сказал, что прокси есть.
    """
    if behind_proxy and forwarded:
        # Прокси дописывают адреса справа; самый левый — исходный клиент.
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return (address or "").strip()


def plural(count: int, one: str, few: str, many: str) -> str:
    """Русское склонение после числа: 1 ролик, 2 ролика, 5 роликов.

    Сообщения о лимитах человек видит в момент, когда ему уже отказали, — и
    корявая грамматика в этот момент читается как «сайт сделан на коленке».
    """
    tail, hundred = count % 10, count % 100
    if tail == 1 and hundred != 11:
        return one
    if 2 <= tail <= 4 and not 12 <= hundred <= 14:
        return few
    return many


def _human_size(size: int) -> str:
    if size >= 1024 ** 3:
        value = size / 1024 ** 3
        return f"{value:.0f} ГБ" if value >= 10 else f"{value:.1f} ГБ"
    return f"{size / 1024 ** 2:.0f} МБ"


def _human_time(seconds: float) -> str:
    total = int(seconds)
    if total >= 3600:
        hours = total // 3600
        minutes = total % 3600 // 60
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    if total >= 60:
        return f"{total // 60} мин"
    return f"{max(total, 1)} с"


__all__ = ["Guard", "LimitError", "Limits", "RateCounter", "client_ip"]
