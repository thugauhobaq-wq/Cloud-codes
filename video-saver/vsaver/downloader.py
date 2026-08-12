"""Загрузчик: тонкая обёртка над `yt-dlp`.

Разбирать чужие видеоплощадки своими руками бессмысленно — они меняются каждый
месяц, и половина такой работы состоит в обходе чужой защиты, чего мы делать не
собираемся. Поэтому вся работа со ссылками отдана `yt-dlp`, а здесь остаётся
ровно четыре вещи: выбор формата, прогресс, отмена и перевод ошибок с
английского технического на человеческий русский.

Наружу модуль показывает протокол `Downloader` с двумя методами. Тесты
подставляют вместо настоящего загрузчика заглушку и работают без сети — так же,
как в `pdf-translator` тесты обходятся без переводчика.

Чего здесь нет намеренно: подстановки чужих cookie, обхода возрастных и платных
барьеров, скачивания защищённых DRM потоков. Сервис берёт то, что площадка и так
отдаёт обычному посетителю.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .store import AUDIO, VIDEO

# Качества, которые предлагаются в интерфейсе. Ключ приходит из браузера,
# значение — потолок высоты кадра в пикселях.
QUALITIES: dict[str, str] = {
    "1080": "до 1080p",
    "720": "до 720p",
    "480": "до 480p",
}
DEFAULT_QUALITY = "1080"

AUDIO_FORMAT = "mp3"
SOCKET_TIMEOUT = 30
RETRIES = 3


class Cancelled(Exception):
    """Задачу отменили — поднимается из хука прогресса и гасит закачку."""


class DownloadError(Exception):
    """Скачать не вышло. Текст уже пригоден для показа человеку."""


@dataclass(slots=True)
class Meta:
    """Что известно о ролике до скачивания."""

    title: str = ""
    duration: int = 0
    size: int = 0
    ext: str = ""
    site: str = ""


@dataclass(slots=True)
class Progress:
    """Состояние закачки в моменте."""

    downloaded: int = 0
    total: int = 0
    speed: int = 0
    eta: int = 0
    stage: str = "download"

    @property
    def percent(self) -> int:
        if self.total <= 0:
            return 0
        return max(0, min(100, int(self.downloaded * 100 / self.total)))


@dataclass(slots=True)
class Result:
    """Что получилось."""

    path: Path
    title: str = ""
    ext: str = ""
    size: int = 0
    duration: int = 0


Report = Callable[[Progress], None]
ShouldStop = Callable[[], bool]


class Downloader(Protocol):
    """Минимум, который нужен очереди. Всё остальное — детали реализации."""

    def probe(self, url: str, *, kind: str, quality: str) -> Meta:
        """Спросить площадку о ролике, ничего не скачивая."""

    def fetch(
        self,
        url: str,
        target: Path,
        *,
        kind: str,
        quality: str,
        report: Report,
        should_stop: ShouldStop,
    ) -> Result:
        """Скачать в `target` (без расширения — его подставит загрузчик)."""


class YtDlp:
    """Настоящий загрузчик поверх `yt_dlp`."""

    def __init__(self, *, max_size: int = 0, socket_timeout: int = SOCKET_TIMEOUT) -> None:
        self.max_size = max_size
        self.socket_timeout = socket_timeout

    # --- опрос -------------------------------------------------------------

    def probe(self, url: str, *, kind: str = VIDEO, quality: str = DEFAULT_QUALITY) -> Meta:
        module = _module()
        options = self._options(kind, quality)
        try:
            with module.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as error:
            raise DownloadError(explain(error)) from error
        if not info:
            raise DownloadError("по этой ссылке ничего не нашлось")
        info = _single(info)
        return Meta(
            title=_title(info),
            duration=int(info.get("duration") or 0),
            size=expected_size(info),
            ext=AUDIO_FORMAT if kind == AUDIO else (info.get("ext") or "mp4"),
            site=(info.get("extractor_key") or "").lower(),
        )

    # --- скачивание --------------------------------------------------------

    def fetch(
        self,
        url: str,
        target: Path,
        *,
        kind: str = VIDEO,
        quality: str = DEFAULT_QUALITY,
        report: Report,
        should_stop: ShouldStop,
    ) -> Result:
        module = _module()
        state = Progress()

        def hook(event: dict) -> None:
            if should_stop():
                raise Cancelled
            status = event.get("status")
            if status == "downloading":
                state.stage = "download"
                state.downloaded = int(event.get("downloaded_bytes") or 0)
                state.total = int(
                    event.get("total_bytes") or event.get("total_bytes_estimate") or 0
                )
                state.speed = int(event.get("speed") or 0)
                state.eta = int(event.get("eta") or 0)
                report(state)
            elif status == "finished":
                # Между «скачано» и «готово» бывает ещё склейка дорожек
                # через ffmpeg: она не мгновенная, и человеку надо это сказать.
                state.stage = "convert"
                report(state)

        def postprocess(event: dict) -> None:
            if should_stop():
                raise Cancelled
            if event.get("status") == "started":
                state.stage = "convert"
                report(state)

        options = self._options(kind, quality)
        options.update(
            {
                "outtmpl": {"default": f"{target}.%(ext)s"},
                "progress_hooks": [hook],
                "postprocessor_hooks": [postprocess],
                "overwrites": True,
            }
        )

        try:
            with module.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
        except Cancelled:
            _clean_partials(target)
            raise
        except Exception as error:
            _clean_partials(target)
            raise DownloadError(explain(error)) from error

        if not info:
            raise DownloadError("площадка не отдала файл")
        info = _single(info)
        path = _downloaded_path(info, target)
        if path is None:
            raise DownloadError("файл скачался, но найти его не удалось")

        state.stage = "done"
        report(state)
        return Result(
            path=path,
            title=_title(info),
            ext=path.suffix.lstrip("."),
            size=path.stat().st_size,
            duration=int(info.get("duration") or 0),
        )

    # --- настройки ---------------------------------------------------------

    def _options(self, kind: str, quality: str) -> dict:
        options: dict = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,  # ссылка на ролик внутри плейлиста — берём ролик
            "playlist_items": "1",
            "socket_timeout": self.socket_timeout,
            "retries": RETRIES,
            "fragment_retries": RETRIES,
            "consoletitle": False,
            "writethumbnail": False,
            "writesubtitles": False,
            "writeinfojson": False,
            "cachedir": False,
            # Ни чужих cookie, ни браузерного профиля: сервис берёт только то,
            # что площадка показывает любому посетителю без входа.
            "cookiefile": None,
            "cookiesfrombrowser": None,
        }
        if self.max_size:
            # Первый рубеж — сам yt-dlp; второй, на реальных байтах, стоит в очереди.
            options["max_filesize"] = self.max_size
        if kind == AUDIO:
            options["format"] = "bestaudio/best"
            options["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": AUDIO_FORMAT,
                    "preferredquality": "192",
                }
            ]
        else:
            options["format"] = _video_format(quality)
            options["merge_output_format"] = "mp4"
        return options


def _video_format(quality: str) -> str:
    """Селектор формата: mp4 предпочтительнее, но не любой ценой.

    Порядок такой: сначала лучшее видео с лучшим звуком в mp4, потом любой
    готовый mp4-файл, потом то же самое без оглядки на контейнер. Последний
    вариант нужен площадкам, где mp4 просто нет, — иначе человек получит
    «формат не найден» на ровном месте.
    """
    height = quality if quality in QUALITIES else DEFAULT_QUALITY
    limit = f"[height<={height}]"
    return (
        f"bv*{limit}[ext=mp4]+ba[ext=m4a]/"
        f"b{limit}[ext=mp4]/"
        f"bv*{limit}+ba/"
        f"b{limit}/"
        "bv*+ba/b"
    )


def expected_size(info: dict) -> int:
    """Сколько это будет весить, по мнению площадки.

    Точного числа до скачивания не знает никто: у потокового видео размер часто
    только приблизительный, а при склейке дорожек надо сложить обе. Поэтому
    оценка используется только для отсева заведомо огромного, а настоящий
    потолок проверяется по факту скачанного.
    """
    formats = info.get("requested_formats")
    if isinstance(formats, list) and formats:
        total = 0
        for item in formats:
            total += int(item.get("filesize") or item.get("filesize_approx") or 0)
        if total:
            return total
    return int(info.get("filesize") or info.get("filesize_approx") or 0)


# Приписка, которую yt-dlp добавляет почти к любой ошибке: просьба сообщить о
# проблеме и обновиться. Её надо срезать до разбора, иначе она сама себя выдаёт
# за причину — например, слово «template» внутри неё содержит «age», и ошибка
# сети превращается в «возрастное ограничение».
BOILERPLATE = (
    "please report this issue",
    "confirm you are on the latest version",
    "filling out the appropriate issue template",
)

# Узнаваемые куски и то, что они значат. Порядок важен: сначала частное, потом
# общее. Каждый образец должен быть достаточно длинным, чтобы не встретиться в
# постороннем тексте, — короткое слово рано или поздно совпадёт не с тем.
REASONS = (
    ("this video is private", "ролик приватный — площадка не отдаёт его никому со стороны"),
    ("video is private", "ролик приватный — площадка не отдаёт его никому со стороны"),
    ("private video", "ролик приватный — площадка не отдаёт его никому со стороны"),
    ("members-only", "ролик только для подписчиков канала"),
    ("join this channel", "ролик только для подписчиков канала"),
    ("premieres in", "трансляция ещё не началась"),
    ("live event will begin", "трансляция ещё не началась"),
    ("is not a valid url", "это не похоже на ссылку"),
    ("unsupported url", "с этой площадкой сервис не работает"),
    ("no video formats found", "по ссылке нет видео — возможно, это страница со списком"),
    ("requested format is not available", "у ролика нет подходящего формата"),
    ("video unavailable", "ролик недоступен: удалён или закрыт по региону"),
    ("not available in your country", "ролик закрыт для страны, где стоит сервер"),
    ("removed by the uploader", "ролик удалён автором"),
    ("copyright", "ролик снят по жалобе правообладателя"),
    ("age-restricted", "ролик с возрастным ограничением — сервис такие не берёт"),
    ("age restricted", "ролик с возрастным ограничением — сервис такие не берёт"),
    ("confirm your age", "ролик с возрастным ограничением — сервис такие не берёт"),
    ("not a bot", "площадка требует подтвердить, что запрос не от робота"),
    ("sign in to confirm", "площадка требует подтвердить, что запрос не от робота"),
    ("sign in", "площадка требует войти в аккаунт — сервис этого не делает"),
    ("login required", "площадка требует войти в аккаунт — сервис этого не делает"),
    ("drm", "поток защищён DRM — сервис такие не берёт"),
    ("file is larger", "файл больше допустимого"),
    ("max-filesize", "файл больше допустимого"),
    ("unable to connect to proxy", "сервер не смог выйти в интернет"),
    ("timed out", "площадка не ответила вовремя"),
    ("temporary failure in name resolution", "сайт не нашёлся"),
    ("name or service not known", "сайт не нашёлся"),
    ("connection refused", "сайт не отвечает"),
    ("http error 404", "страница не найдена"),
    ("http error 403", "площадка закрыла доступ к этой ссылке"),
    ("http error 429", "площадка временно ограничила запросы с сервера"),
    ("ffmpeg not found", "не удалось собрать файл: на сервере не хватает ffmpeg"),
    ("ffprobe and ffmpeg not found", "не удалось собрать файл: на сервере не хватает ffmpeg"),
    ("unable to download webpage", "страница не открылась"),
)


def strip_boilerplate(text: str) -> str:
    """Убрать из сообщения всё, начиная с просьбы сообщить о проблеме."""
    low = text.lower()
    edge = len(text)
    for marker in BOILERPLATE:
        found = low.find(marker)
        if found >= 0:
            edge = min(edge, found)
    return text[:edge].strip(" ;.,")


def explain(error: Exception) -> str:
    """Ошибку `yt-dlp` — в одну понятную фразу.

    Текст оригинала английский, длинный и с внутренними подробностями вроде
    имени экстрактора и ссылки на трекер. Показывать его человеку нельзя, а
    знать причину надо, поэтому разбираем по узнаваемым кускам.
    """
    text = strip_boilerplate(str(error))
    low = text.lower()
    for needle, message in REASONS:
        if needle in low:
            return message
    # Ничего не узнали — отдаём хвост оригинала, срезав служебный префикс.
    tail = text.split("ERROR:")[-1].strip()
    return f"скачать не удалось: {tail[:200]}" if tail else "скачать не удалось"


def _module():
    try:
        import yt_dlp
    except ImportError as error:  # pragma: no cover — есть в образе и в CI
        raise DownloadError(
            "на сервере не установлен yt-dlp — сервис не может ничего скачать"
        ) from error
    return yt_dlp


def _single(info: dict) -> dict:
    """Из плейлиста взять первую запись: `noplaylist` помогает не всегда."""
    entries = info.get("entries")
    if isinstance(entries, list) and entries:
        first = entries[0]
        if isinstance(first, dict):
            return first
    return info


def _title(info: dict) -> str:
    return str(info.get("title") or info.get("id") or "видео").strip()


def _downloaded_path(info: dict, target: Path) -> Path | None:
    """Куда лёг файл. Сначала спрашиваем yt-dlp, потом ищем сами."""
    requested = info.get("requested_downloads")
    if isinstance(requested, list) and requested:
        name = requested[0].get("filepath") or requested[0].get("_filename")
        if name and Path(name).is_file():
            return Path(name)
    name = info.get("filepath") or info.get("_filename")
    if name and Path(name).is_file():
        return Path(name)
    for path in sorted(target.parent.glob(f"{target.name}.*")):
        if path.is_file() and path.suffix != ".part":
            return path
    return None


def _clean_partials(target: Path) -> None:
    """Убрать недокачанное: на открытом сервисе мусор копится быстро."""
    for path in target.parent.glob(f"{target.name}*"):
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass


__all__ = [
    "AUDIO_FORMAT",
    "DEFAULT_QUALITY",
    "QUALITIES",
    "Cancelled",
    "DownloadError",
    "Downloader",
    "Meta",
    "Progress",
    "Report",
    "Result",
    "ShouldStop",
    "YtDlp",
    "expected_size",
    "explain",
]
