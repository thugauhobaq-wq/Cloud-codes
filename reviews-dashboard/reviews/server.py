"""Веб-сервер дашборда: JSON-API + отдача статики. Только stdlib."""

from __future__ import annotations

import json
import mimetypes
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .analytics import summarize
from .httpclient import FetchError
from .pipeline import collect
from .sources import describe_sources
from .storage import DEFAULT_DB, Storage

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
MAX_BODY = 64 * 1024
PAGE_SIZE_LIMIT = 200


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "ReviewsDashboard/1.0"
    db_path: str = str(DEFAULT_DB)

    # ------------------------------------------------------------- маршруты

    def do_GET(self) -> None:  # noqa: N802 — имя задано BaseHTTPRequestHandler
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        params = {key: value[0] for key, value in urllib.parse.parse_qs(parsed.query).items()}

        try:
            if route == "/api/sources":
                self._json({"sources": describe_sources()})
            elif route == "/api/companies":
                with self._storage() as storage:
                    self._json({"companies": storage.companies()})
            elif route == "/api/summary":
                self._json(self._summary(params))
            elif route == "/api/reviews":
                self._json(self._reviews(params))
            else:
                self._static(parsed.path)
        except FileNotFoundError:
            self._json({"error": "не найдено"}, status=404)
        except Exception as error:  # noqa: BLE001 — сервер не должен падать
            self._json({"error": str(error)}, status=500)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        if route != "/api/collect":
            self._json({"error": "не найдено"}, status=404)
            return

        try:
            payload = self._body()
            source = str(payload.get("source") or "").strip()
            if not source:
                raise ValueError("не указан источник")
            limit = max(1, min(int(payload.get("limit") or 100), 1000))
            with self._storage() as storage:
                stats = collect(
                    source,
                    str(payload.get("query") or ""),
                    limit=limit,
                    company=(payload.get("company") or None),
                    storage=storage,
                    from_file=payload.get("from_file") or None,
                    **({"api_key": payload["api_key"]} if payload.get("api_key") else {}),
                )
            self._json(stats)
        except (FetchError, KeyError, ValueError, OSError) as error:
            self._json({"error": str(error)}, status=400)
        except Exception as error:  # noqa: BLE001
            self._json({"error": str(error)}, status=500)

    # ------------------------------------------------------------ обработчики

    def _summary(self, params: dict[str, str]) -> dict[str, Any]:
        with self._storage() as storage:
            rows = storage.fetch(
                company=params.get("company") or None,
                source=params.get("source") or None,
                sentiment=params.get("sentiment") or None,
                query=params.get("q") or None,
                date_from=params.get("from") or None,
            )
            companies = storage.companies()
        data = summarize(rows)
        data["companies"] = companies
        data["filters"] = params
        return data

    def _reviews(self, params: dict[str, str]) -> dict[str, Any]:
        try:
            limit = min(int(params.get("limit", 30)), PAGE_SIZE_LIMIT)
            offset = max(0, int(params.get("offset", 0)))
        except ValueError:
            limit, offset = 30, 0

        filters = {
            "company": params.get("company") or None,
            "source": params.get("source") or None,
            "sentiment": params.get("sentiment") or None,
            "query": params.get("q") or None,
            "date_from": params.get("from") or None,
        }
        with self._storage() as storage:
            rows = storage.fetch(limit=limit, offset=offset, **filters)
            total = storage.total_matching(**filters)
        return {"reviews": rows, "total": total, "limit": limit, "offset": offset}

    # ----------------------------------------------------------------- утилиты

    def _storage(self) -> Storage:
        return Storage(self.db_path)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("слишком большой запрос")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError("тело запроса не является JSON") from error
        return data if isinstance(data, dict) else {}

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path: str) -> None:
        relative = path.lstrip("/") or "index.html"
        target = (WEB_DIR / relative).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
            raise FileNotFoundError(relative)

        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript",):
            content_type += "; charset=utf-8"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        # Короткий однострочный лог вместо стандартного многословного.
        print(f"[{self.log_date_time_string()}] {fmt % args}")


def run_server(host: str = "127.0.0.1", port: int = 8000, db_path: str | Path = DEFAULT_DB) -> None:
    handler = type("BoundHandler", (DashboardHandler,), {"db_path": str(db_path)})
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"Дашборд отзывов: http://{host}:{port}  (база: {db_path})")
    print("Остановить — Ctrl+C")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        httpd.server_close()
