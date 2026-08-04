"""Аккуратный HTTP-клиент на urllib: заголовки браузера, ретраи, пауза, кэш."""

from __future__ import annotations

import gzip
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Any

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "close",
}


class FetchError(RuntimeError):
    """Не удалось получить данные от источника."""


def _decode(raw: bytes, encoding: str) -> str:
    if encoding == "gzip":
        raw = gzip.decompress(raw)
    elif encoding == "deflate":
        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw.decode("utf-8", errors="replace")


def fetch(
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    data: bytes | dict[str, Any] | None = None,
    timeout: float = 20.0,
    retries: int = 3,
    pause: float = 0.7,
) -> str:
    """GET/POST с ретраями и экспоненциальной паузой. Возвращает тело ответа."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"

    request_headers = dict(DEFAULT_HEADERS)
    if headers:
        request_headers.update(headers)

    body: bytes | None = None
    if isinstance(data, dict):
        body = json.dumps(data, ensure_ascii=False).encode()
        request_headers.setdefault("Content-Type", "application/json")
    elif isinstance(data, bytes):
        body = data

    last_error: Exception | None = None
    for attempt in range(retries):
        if attempt:
            time.sleep(pause * (2 ** attempt) + random.uniform(0, 0.4))
        try:
            request = urllib.request.Request(url, data=body, headers=request_headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return _decode(response.read(), response.headers.get("Content-Encoding", ""))
        except urllib.error.HTTPError as error:
            last_error = error
            if error.code in (400, 401, 403, 404, 410):
                break  # ретраи не помогут
        except (urllib.error.URLError, TimeoutError, OSError, zlib.error) as error:
            last_error = error

    raise FetchError(f"{url}: {last_error}")


def fetch_json(url: str, **kwargs: Any) -> Any:
    """То же, что fetch, но с разбором JSON."""
    raw = fetch(url, **kwargs)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        snippet = raw[:200].replace("\n", " ")
        raise FetchError(f"{url}: ответ не является JSON ({snippet})") from error


def polite_sleep(seconds: float = 0.8) -> None:
    """Пауза между страницами, чтобы не долбить чужой сайт."""
    time.sleep(seconds + random.uniform(0, 0.3))
