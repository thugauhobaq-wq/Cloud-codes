"""HTTP-слой: заголовки, ретраи, реакция на коды ответа."""

from __future__ import annotations

import httpx
import pytest
import respx

from fparser.sources.base import USER_AGENTS, Fetcher, FetchError

URL = "https://example.com/feed"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Ретраи ждут секундами — в тестах это лишнее."""

    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("fparser.sources.base.asyncio.sleep", instant)


@respx.mock
async def test_sends_a_browser_user_agent():
    route = respx.get(URL).mock(return_value=httpx.Response(200, content=b"ok"))
    fetcher = Fetcher()

    await fetcher.get(URL)
    await fetcher.aclose()

    assert route.calls.last.request.headers["User-Agent"] in USER_AGENTS


@respx.mock
async def test_source_headers_are_merged_not_dropped():
    """Источник вправе добавить свой заголовок — Kwork шлёт Referer. Раньше
    он конфликтовал с User-Agent и запрос падал, не уходя в сеть."""
    route = respx.get(URL).mock(return_value=httpx.Response(200, content=b"ok"))
    fetcher = Fetcher()

    await fetcher.get(URL, headers={"Referer": "https://example.com/"})
    await fetcher.aclose()

    request = route.calls.last.request
    assert request.headers["Referer"] == "https://example.com/"
    assert request.headers["User-Agent"] in USER_AGENTS


@respx.mock
async def test_retries_server_error_then_succeeds():
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(500), httpx.Response(200, content=b"ok")]
    )
    fetcher = Fetcher(max_attempts=3)

    response = await fetcher.get(URL)
    await fetcher.aclose()

    assert response.content == b"ok"
    assert route.call_count == 2


@respx.mock
async def test_gives_up_after_max_attempts():
    respx.get(URL).mock(return_value=httpx.Response(503))
    fetcher = Fetcher(max_attempts=2)

    with pytest.raises(FetchError) as exc_info:
        await fetcher.get(URL)
    await fetcher.aclose()

    assert exc_info.value.status_code == 503
    assert not exc_info.value.is_blocked


@respx.mock
async def test_client_error_is_not_retried():
    """404 не станет 200 от повторов — это только лишний стук в биржу."""
    route = respx.get(URL).mock(return_value=httpx.Response(404))
    fetcher = Fetcher(max_attempts=3)

    with pytest.raises(FetchError):
        await fetcher.get(URL)
    await fetcher.aclose()

    assert route.call_count == 1


@respx.mock
@pytest.mark.parametrize("status", [403, 429])
async def test_blocking_statuses_are_flagged(status: int):
    """403 и 429 требуют отступить, а не просто повторить."""
    respx.get(URL).mock(return_value=httpx.Response(status))
    fetcher = Fetcher(max_attempts=1)

    with pytest.raises(FetchError) as exc_info:
        await fetcher.get(URL)
    await fetcher.aclose()

    assert exc_info.value.is_blocked


@respx.mock
async def test_network_error_is_wrapped():
    respx.get(URL).mock(side_effect=httpx.ConnectError("нет сети"))
    fetcher = Fetcher(max_attempts=1)

    with pytest.raises(FetchError) as exc_info:
        await fetcher.get(URL)
    await fetcher.aclose()

    assert exc_info.value.status_code is None
    assert not exc_info.value.is_blocked


def test_retry_after_header_is_respected():
    response = httpx.Response(429, headers={"Retry-After": "12"})

    assert Fetcher._retry_delay(response, attempt=1) == 12.0


def test_absurd_retry_after_is_capped():
    """Некоторые сервисы присылают «через сутки» — столько ждать нельзя."""
    response = httpx.Response(429, headers={"Retry-After": "86400"})

    assert Fetcher._retry_delay(response, attempt=1) == 120.0


def test_retry_after_date_format_falls_back_to_backoff():
    response = httpx.Response(429, headers={"Retry-After": "Tue, 28 Jul 2026 04:00:00 GMT"})

    assert Fetcher._retry_delay(response, attempt=2) == 4.0
