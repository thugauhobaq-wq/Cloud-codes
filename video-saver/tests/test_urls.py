"""Проверка ссылок — единственная защита от того, чтобы сервер сходил не туда."""

from __future__ import annotations

import pytest

from vsaver.urls import UrlError, check, clean, site_of


class TestClean:
    def test_adds_scheme(self):
        assert clean("youtube.com/watch?v=1").startswith("https://")

    def test_strips_messenger_brackets(self):
        assert clean("  <https://example.com/v>  ") == "https://example.com/v"

    def test_empty_is_rejected(self):
        with pytest.raises(UrlError):
            clean("   ")

    def test_newline_is_rejected(self):
        # Перенос строки в ссылке — верный признак, что скопировалось лишнее.
        with pytest.raises(UrlError):
            clean("https://example.com/v\nhttps://example.com/w")

    def test_absurdly_long_is_rejected(self):
        with pytest.raises(UrlError):
            clean("https://example.com/" + "x" * 3000)


class TestScheme:
    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "ftp://example.com/v.mp4",
            "gopher://example.com/",
            "javascript:alert(1)",
        ],
    )
    def test_only_http_and_https(self, url):
        with pytest.raises(UrlError, match="http"):
            check(url, resolve=False)

    def test_credentials_are_rejected(self):
        # Ссылка с логином внутри — обычно попытка подсунуть чужой доступ.
        with pytest.raises(UrlError, match="логином"):
            check("https://user:secret@example.com/v", resolve=False)

    def test_no_host(self):
        with pytest.raises(UrlError):
            check("https:///path", resolve=False)


class TestPrivateAddresses:
    """Главное в модуле: сервер не должен ходить внутрь своей же сети."""

    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            "localhost",  # разрешается в 127.0.0.1 через настоящий getaddrinfo
            "10.0.0.5",
            "192.168.1.1",
            "172.16.9.9",
            "169.254.169.254",  # служба метаданных облака: там лежат ключи
            "0.0.0.0",
            "[::1]",
            "[fd00::1]",
            "[::ffff:127.0.0.1]",  # IPv4 в обёртке IPv6 — is_global тут врёт
        ],
    )
    def test_rejected(self, host):
        with pytest.raises(UrlError, match="внутрь сети"):
            check(f"https://{host}/video.mp4")

    def test_public_address_passes(self):
        target = check("https://93.184.216.34/video.mp4")
        assert target.host == "93.184.216.34"

    def test_unknown_host(self):
        with pytest.raises(UrlError, match="не нашёлся"):
            check("https://xn--this-name-does-not-exist-1234567890.invalid/v")

    def test_resolve_can_be_turned_off(self):
        # Без обращения к DNS проверка адреса не делается вовсе.
        assert check("https://127.0.0.1/v", resolve=False).host == "127.0.0.1"


class TestNormalisation:
    def test_playlist_is_dropped(self):
        """Ссылка на ролик внутри плейлиста должна остаться ссылкой на ролик."""
        target = check(
            "https://www.youtube.com/watch?v=abc&list=PL123&index=4", resolve=False
        )
        assert "list=" not in target.url
        assert "index=" not in target.url
        assert "v=abc" in target.url

    def test_tracking_marks_are_dropped(self):
        target = check(
            "https://example.com/v?id=7&utm_source=tg&fbclid=xxx&si=zz", resolve=False
        )
        assert target.url == "https://example.com/v?id=7"

    def test_fragment_is_dropped(self):
        assert check("https://example.com/v#t=30", resolve=False).url.endswith("/v")

    def test_useful_params_survive(self):
        target = check("https://example.com/v?id=7&t=90", resolve=False)
        assert "id=7" in target.url and "t=90" in target.url


class TestSite:
    @pytest.mark.parametrize(
        ("host", "expected"),
        [
            ("www.youtube.com", "youtube.com"),
            ("m.vk.com", "vk.com"),
            ("VIMEO.COM", "vimeo.com"),
            ("rutube.ru", "rutube.ru"),
        ],
    )
    def test_short_name(self, host, expected):
        assert site_of(host) == expected

    def test_taken_from_url(self):
        assert check("https://www.Example.com/v", resolve=False).site == "example.com"
