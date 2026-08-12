"""Загрузчик: выбор формата, оценка размера и перевод ошибок на человеческий.

Сети здесь нет: настоящий `yt-dlp` никуда не ходит, проверяются только те
куски, которые пишем мы, — селектор формата, арифметика размера и разбор
сообщений об ошибках.
"""

from __future__ import annotations

import pytest

from vsaver.downloader import (
    DEFAULT_QUALITY,
    QUALITIES,
    Progress,
    YtDlp,
    _video_format,
    expected_size,
    explain,
    strip_boilerplate,
)

# Приписка, которой yt-dlp заканчивает почти каждую ошибку.
TAIL = (
    "; please report this issue on  https://github.com/yt-dlp/yt-dlp/issues?q= , "
    "filling out the appropriate issue template. Confirm you are on the latest "
    "version using  yt-dlp -U"
)


class TestFormat:
    @pytest.mark.parametrize("quality", sorted(QUALITIES))
    def test_height_is_limited(self, quality):
        assert f"height<={quality}" in _video_format(quality)

    def test_unknown_quality_falls_back(self):
        assert f"height<={DEFAULT_QUALITY}" in _video_format("4320")

    def test_mp4_is_preferred_but_not_required(self):
        """Если mp4 у площадки нет, человек должен получить хоть что-то."""
        selector = _video_format("720")
        assert selector.startswith("bv*[height<=720][ext=mp4]")
        assert selector.endswith("bv*+ba/b")


class TestOptions:
    def test_no_cookies_ever(self):
        """Ни чужих cookie, ни браузерного профиля — это принципиально."""
        options = YtDlp()._options("video", "1080")
        assert options["cookiefile"] is None
        assert options["cookiesfrombrowser"] is None

    def test_playlist_is_off(self):
        options = YtDlp()._options("video", "1080")
        assert options["noplaylist"] is True
        assert options["playlist_items"] == "1"

    def test_size_limit_is_passed_down(self):
        assert YtDlp(max_size=1234)._options("video", "1080")["max_filesize"] == 1234

    def test_no_limit_means_no_option(self):
        assert "max_filesize" not in YtDlp(max_size=0)._options("video", "1080")

    def test_audio_asks_for_mp3(self):
        options = YtDlp()._options("audio", "1080")
        assert options["postprocessors"][0]["preferredcodec"] == "mp3"
        assert "format" in options and "height" not in options["format"]

    def test_nothing_extra_is_written(self):
        """Обложки, субтитры и json рядом с видео нам не нужны — это мусор на диске."""
        options = YtDlp()._options("video", "1080")
        assert not options["writethumbnail"]
        assert not options["writesubtitles"]
        assert not options["writeinfojson"]


class TestExpectedSize:
    def test_merged_streams_are_summed(self):
        """Видео и звук качаются отдельно — размер надо складывать."""
        info = {"requested_formats": [{"filesize": 1000}, {"filesize": 200}]}
        assert expected_size(info) == 1200

    def test_approximate_size_is_used(self):
        assert expected_size({"filesize_approx": 5000}) == 5000

    def test_exact_size_wins(self):
        assert expected_size({"filesize": 900, "filesize_approx": 5000}) == 900

    def test_unknown_size_is_zero(self):
        # Площадки часто не говорят размер заранее — это нормально.
        assert expected_size({}) == 0

    def test_partial_stream_sizes(self):
        info = {"requested_formats": [{"filesize": 1000}, {}]}
        assert expected_size(info) == 1000


class TestProgress:
    def test_percent(self):
        assert Progress(downloaded=50, total=200).percent == 25

    def test_unknown_total_gives_zero(self):
        assert Progress(downloaded=50, total=0).percent == 0

    def test_percent_never_exceeds_hundred(self):
        # Оценка размера бывает заниженной, а проценты выше ста пугают.
        assert Progress(downloaded=300, total=200).percent == 100


class TestExplain:
    """Главное здесь — не перепутать причину.

    Образцы ищутся подстрокой, а к каждой ошибке yt-dlp дописывает длинную
    приписку про баг-трекер. Одно короткое слово в списке образцов — и любая
    ошибка сети начинает объясняться как возрастное ограничение.
    """

    def test_boilerplate_is_stripped(self):
        assert strip_boilerplate("ERROR: что-то сломалось" + TAIL) == "ERROR: что-то сломалось"

    def test_network_error_is_not_mistaken_for_age_limit(self):
        """Слово «template» в приписке содержит «age» — на этом легко обжечься."""
        error = Exception(
            "ERROR: [generic] video: Unable to download webpage: "
            "('Unable to connect to proxy', OSError('Tunnel connection failed'))" + TAIL
        )
        assert explain(error) == "сервер не смог выйти в интернет"

    def test_boilerplate_alone_does_not_decide(self):
        """На пустой по смыслу ошибке не должно всплыть ни одно объяснение."""
        message = explain(Exception("ERROR: something odd happened" + TAIL))
        assert message.startswith("скачать не удалось")
        assert "issue template" not in message
        assert "github" not in message

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("ERROR: [youtube] abc: Private video. Sign in if you've been granted access",
             "ролик приватный — площадка не отдаёт его никому со стороны"),
            ("ERROR: [youtube] abc: Video unavailable",
             "ролик недоступен: удалён или закрыт по региону"),
            ("ERROR: Unsupported URL: https://example.com/",
             "с этой площадкой сервис не работает"),
            ("ERROR: [youtube] abc: Sign in to confirm you're not a bot",
             "площадка требует подтвердить, что запрос не от робота"),
            ("ERROR: [youtube] abc: Sign in to confirm your age",
             "ролик с возрастным ограничением — сервис такие не берёт"),
            ("ERROR: This video is age-restricted",
             "ролик с возрастным ограничением — сервис такие не берёт"),
            ("ERROR: members-only content", "ролик только для подписчиков канала"),
            ("ERROR: Requested format is not available", "у ролика нет подходящего формата"),
            ("ERROR: unable to download video data: HTTP Error 403: Forbidden",
             "площадка закрыла доступ к этой ссылке"),
            ("ERROR: HTTP Error 429: Too Many Requests",
             "площадка временно ограничила запросы с сервера"),
            ("ERROR: no video formats found!",
             "по ссылке нет видео — возможно, это страница со списком"),
            ("ERROR: ffmpeg not found. Please install",
             "не удалось собрать файл: на сервере не хватает ffmpeg"),
            ("ERROR: The read operation timed out", "площадка не ответила вовремя"),
        ],
    )
    def test_known_reasons(self, raw, expected):
        assert explain(Exception(raw + TAIL)) == expected

    def test_unknown_reason_keeps_the_tail_short(self):
        message = explain(Exception("ERROR: " + "ы" * 500))
        assert message.startswith("скачать не удалось: ")
        assert len(message) < 250
