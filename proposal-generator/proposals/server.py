"""Веб-форма: заполнил поля, нажал «Скачать PDF» — получил файл.

Обычный `http.server` из стандартной библиотеки. Сервер локальный и
однопользовательский: это инструмент для своего компьютера, а не публичный
сервис, поэтому по умолчанию слушает только 127.0.0.1.
"""

from __future__ import annotations

import contextlib
import json
import webbrowser
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote

from . import storage
from .pdf import FontError, find_family
from .pdf.fonts import FontFamily
from .sample import demo_proposal
from .storage import Drafts, StorageError
from .template import TEMPLATES, render

WEB_ROOT = Path(__file__).resolve().parent / "web"
"""Статика лежит внутри пакета — тогда она едет и в колесо, и в editable-установку."""
MAX_BODY = 12 * 1024 * 1024
"""Логотипы приезжают внутри JSON в base64 — с запасом на пару крупных PNG."""

_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "proposals"
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, drafts: Drafts, family: FontFamily | None, **kwargs) -> None:
        self.drafts = drafts
        self.family = family
        super().__init__(*args, **kwargs)

    # --- маршруты ------------------------------------------------------------

    def _path(self) -> str:
        """Путь запроса без строки параметров и с развёрнутыми %XX.

        Имя черновика браузер шлёт через `encodeURIComponent`, так что без
        `unquote` кириллический слаг доехал бы сюда набором процентов.
        """
        return unquote(self.path.split("?", 1)[0])

    def do_GET(self) -> None:
        path = self._path()
        match path:
            case "/" | "/index.html":
                self._static("index.html")
            case "/api/state":
                self._send_json(self._state())
            case "/api/demo":
                self._send_json(storage.to_json(demo_proposal()))
            case "/api/drafts":
                self._send_json({"drafts": [info.to_dict() for info in self.drafts.list()]})
            case _ if path.startswith("/api/drafts/"):
                self._read_draft(path.rsplit("/", 1)[-1])
            case _:
                self._static(path.lstrip("/"))

    def do_POST(self) -> None:
        path = self._path()
        try:
            payload = self._read_json()
        except ValueError as error:
            self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return

        match path:
            case "/api/render":
                self._render(payload)
            case _ if path.startswith("/api/drafts/"):
                self._write_draft(path.rsplit("/", 1)[-1], payload)
            case _:
                self._send_json({"error": "нет такого метода"}, HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:
        path = self._path()
        if not path.startswith("/api/drafts/"):
            self._send_json({"error": "нет такого метода"}, HTTPStatus.NOT_FOUND)
            return
        try:
            removed = self.drafts.delete(path.rsplit("/", 1)[-1])
        except StorageError as error:
            self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"deleted": removed})

    # --- действия ------------------------------------------------------------

    def _state(self) -> dict:
        """Всё, что нужно форме при старте."""
        family = None
        font_error = None
        try:
            family = (self.family or find_family()).name
        except FontError as error:
            font_error = str(error)
        return {
            "templates": TEMPLATES,
            "font": family,
            "font_error": font_error,
            "next_number": self.drafts.next_number(),
            "drafts": [info.to_dict() for info in self.drafts.list()],
        }

    def _render(self, payload: dict) -> None:
        proposal = storage.from_json(payload)
        try:
            data, warnings = render(proposal, family=self.family)
        except FontError as error:
            self._send_json({"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        except ValueError as error:  # битая картинка, странный цвет
            self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'inline; filename="{proposal.filename}"')
        if warnings:
            # Заголовок, а не тело: тело здесь — сам PDF.
            self.send_header("X-Proposal-Warnings", _header_safe("; ".join(warnings)))
        self.end_headers()
        self.wfile.write(data)

    def _read_draft(self, slug: str) -> None:
        try:
            proposal = self.drafts.read(slug)
        except StorageError:
            self._send_json({"error": "черновик не найден"}, HTTPStatus.NOT_FOUND)
            return
        self._send_json(storage.to_json(proposal))

    def _write_draft(self, slug: str, payload: dict) -> None:
        proposal = storage.from_json(payload)
        try:
            path = self.drafts.write(slug, proposal)
        except (StorageError, OSError) as error:
            self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"saved": slug, "path": str(path)})

    # --- служебное -----------------------------------------------------------

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("слишком большой запрос — уменьшите логотип")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"не разобрать JSON: {error}") from error
        if not isinstance(payload, dict):
            raise ValueError("ожидался объект JSON")
        return payload

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, name: str) -> None:
        target = (WEB_ROOT / name).resolve()
        if not target.is_file() or WEB_ROOT.resolve() not in target.parents:
            self.send_error(HTTPStatus.NOT_FOUND, "not found")
            return
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", _TYPES.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        # Стандартный лог печатает каждую картинку и каждый опрос — шумно.
        if args and str(args[0]).startswith(("POST /api/render", "GET /api/state")):
            super().log_message(format, *args)


def _header_safe(text: str) -> str:
    """HTTP-заголовок обязан быть latin-1 — кириллицу отдаём в процентах."""
    return quote(text)


def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    data_dir: str | Path = storage.DEFAULT_DIR,
    font: str | None = None,
    open_browser: bool = True,
) -> None:
    """Запустить форму. Блокирует поток до Ctrl+C."""
    drafts = Drafts(data_dir)
    family: FontFamily | None = None
    try:
        family = find_family(font)
    except FontError as error:
        # Не падаем: форму открыть можно, а про шрифт скажем в интерфейсе.
        print(f"Внимание: {error}")

    handler = partial(Handler, drafts=drafts, family=family)
    server = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}"
    print(f"Форма: {url}")
    print(f"Черновики: {drafts.directory}")
    if family:
        print(f"Шрифт: {family.name}")
    if open_browser:
        # Без графической оболочки открывать нечего — это не ошибка.
        with contextlib.suppress(Exception):
            webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        server.server_close()
