"""HTTP-сервер: сама страница и API к очереди закачек.

Всё на стандартной библиотеке. Отдельно стоит сказать про три вещи, из-за
которых кода здесь больше, чем «прочитать запрос и ответить»:

* Прогресс отдаётся через Server-Sent Events. Это обычный HTTP-ответ, который не
  закрывается, — работает через любой прокси и переживает спящий экран лучше,
  чем веб-сокет, а кода требует втрое меньше.
* Готовый файл отдаётся с поддержкой `Range`. Без неё видео в браузере не
  перематывается, а оборванная закачка начинается с нуля — для сайта про видео
  это не мелочь, а половина смысла.
* Сессия в cookie нужна не для доступа, а чтобы показать человеку его задачи и
  не показывать чужие. Ссылка на готовый файл работает у любого, кто её получил:
  так её можно переслать себе на телефон.
"""

from __future__ import annotations

import contextlib
import json
import mimetypes
import queue
import re
import socket
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .downloader import DEFAULT_QUALITY, QUALITIES
from .jobs import Queue, Settings
from .limits import LimitError, Limits, client_ip
from .store import ACTIVE, AUDIO, DONE, VIDEO, Store, new_owner
from .urls import UrlError

WEB = Path(__file__).parent / "web"
CHUNK = 256 * 1024
MAX_BODY = 8 * 1024
COOKIE = "vsaver"
COOKIE_AGE = 30 * 24 * 3600

JOB_ROUTE = re.compile(r"/api/jobs/([0-9a-f]{6,32})(/[a-z]+)?$")
RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")

MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
}

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


class Application:
    """Сервер целиком: очередь, хранилище и настройки в одном месте."""

    def __init__(
        self,
        data: Path | str = "data",
        *,
        settings: Settings | None = None,
        downloader: object | None = None,
        behind_proxy: bool = False,
    ) -> None:
        self.store = Store(data)
        self.queue = Queue(self.store, settings, downloader)
        self.limits: Limits = self.queue.settings.limits
        self.behind_proxy = behind_proxy

    def start(self) -> None:
        self.queue.start()

    def stop(self) -> None:
        self.queue.stop()
        self.store.close()


class Handler(BaseHTTPRequestHandler):
    """Разбор запросов. Экземпляр приложения приходит через класс сервера."""

    protocol_version = "HTTP/1.1"
    server_version = "vsaver"
    sys_version = ""
    app: Application

    # --- служебное ---------------------------------------------------------

    def log_message(self, fmt: str, *args: object) -> None:
        # Каждая картинка и каждый пинг прогресса в логе — это шум, за которым
        # не видно настоящих событий.
        if self.path.startswith("/api/") and not self.path.endswith("/events"):
            super().log_message(fmt, *args)

    @property
    def client_address_text(self) -> str:
        return client_ip(
            self.client_address[0] if self.client_address else "",
            self.headers.get("X-Forwarded-For", ""),
            behind_proxy=self.app.behind_proxy,
        )

    def _owner(self) -> str:
        """Номер сессии из cookie. Новый заводится при первом обращении."""
        raw = self.headers.get("Cookie", "")
        if raw:
            jar = SimpleCookie()
            with contextlib.suppress(Exception):
                jar.load(raw)
            value = jar[COOKIE].value if COOKIE in jar else ""
            if re.fullmatch(r"[0-9a-f]{32}", value or ""):
                return value
        return ""

    def _owner_or_new(self) -> tuple[str, dict[str, str]]:
        """Сессия и заголовки, которые надо дослать, если она только что заведена."""
        owner = self._owner()
        if owner:
            return owner, {}
        owner = new_owner()
        secure = "; Secure" if self._is_https() else ""
        return owner, {
            "Set-Cookie": (
                f"{COOKIE}={owner}; Path=/; Max-Age={COOKIE_AGE}; "
                f"HttpOnly; SameSite=Lax{secure}"
            )
        }

    def _is_https(self) -> bool:
        return self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def _send(
        self,
        status: HTTPStatus | int,
        body: bytes = b"",
        content_type: str = "application/json; charset=utf-8",
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD" and body:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(body)

    def _json(
        self,
        payload: object,
        status: HTTPStatus | int = HTTPStatus.OK,
        extra: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", extra)

    def _error(self, status: HTTPStatus | int, message: str, retry: int = 0) -> None:
        extra = {"Retry-After": str(retry)} if retry else None
        self._json({"error": message}, status, extra)

    # --- маршруты ----------------------------------------------------------

    def do_GET(self) -> None:
        url = urlparse(self.path)
        path = url.path
        params = parse_qs(url.query)
        try:
            if path == "/api/config":
                return self._config()
            if path == "/api/jobs":
                return self._my_jobs()
            match = JOB_ROUTE.fullmatch(path)
            if match:
                return self._job_route(match.group(1), match.group(2), params)
            if path.startswith("/api/"):
                return self._error(HTTPStatus.NOT_FOUND, "нет такого адреса")
            return self._static(path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"ошибка сервера: {error}")

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        try:
            if urlparse(self.path).path == "/api/jobs":
                return self._create_job()
            return self._error(HTTPStatus.NOT_FOUND, "нет такого адреса")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"ошибка сервера: {error}")

    def do_DELETE(self) -> None:
        match = JOB_ROUTE.fullmatch(urlparse(self.path).path)
        if not match or match.group(2):
            return self._error(HTTPStatus.NOT_FOUND, "нет такого адреса")
        job_id = match.group(1)
        job = self.app.store.get(job_id)
        if job is None:
            return self._json({"deleted": False})
        # Удалять можно только свои задачи: чужую отменить с улицы нельзя.
        if job.owner and job.owner != self._owner():
            return self._error(HTTPStatus.FORBIDDEN, "это не ваша задача")
        return self._json({"deleted": self.app.queue.delete(job_id)})

    # --- обработчики -------------------------------------------------------

    def _config(self) -> None:
        limits = self.app.limits
        self._json(
            {
                "qualities": QUALITIES,
                "defaultQuality": DEFAULT_QUALITY,
                "limits": limits.as_dict(),
                "version": 1,
            }
        )

    def _my_jobs(self) -> None:
        owner = self._owner()
        jobs = self.app.store.recent(50, owner=owner) if owner else []
        self._json([job.as_dict() for job in jobs])

    def _create_job(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return self._error(HTTPStatus.LENGTH_REQUIRED, "пустой запрос")
        if length > MAX_BODY:
            return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "слишком длинный запрос")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._error(HTTPStatus.BAD_REQUEST, "запрос не разобрался")
        if not isinstance(payload, dict):
            return self._error(HTTPStatus.BAD_REQUEST, "запрос не разобрался")

        kind = AUDIO if str(payload.get("kind", "")).strip() == AUDIO else VIDEO
        quality = str(payload.get("quality", "") or DEFAULT_QUALITY).strip()
        if quality not in QUALITIES:
            quality = DEFAULT_QUALITY
        owner, cookie = self._owner_or_new()

        try:
            job = self.app.queue.submit(
                str(payload.get("url", "")),
                kind=kind,
                quality=quality,
                owner=owner,
                ip=self.client_address_text,
            )
        except UrlError as error:
            return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST, cookie)
        except LimitError as error:
            extra = dict(cookie)
            if error.retry_after:
                extra["Retry-After"] = str(error.retry_after)
            return self._json({"error": str(error)}, error.status, extra)
        return self._json(job.as_dict(), HTTPStatus.ACCEPTED, cookie)

    def _job_route(self, job_id: str, tail: str | None, params: dict) -> None:
        job = self.app.store.get(job_id)
        if job is None:
            return self._error(HTTPStatus.NOT_FOUND, "задача не найдена")
        if tail is None:
            return self._json(job.as_dict())
        if tail == "/events":
            return self._events(job_id)
        if tail == "/file":
            if job.status != DONE:
                return self._error(HTTPStatus.CONFLICT, "файл ещё не готов")
            path = self.app.store.find_file(job_id)
            if path is None:
                return self._error(HTTPStatus.GONE, "файл уже удалён с сервера")
            inline = params.get("inline", ["0"])[0] == "1"
            return self._file(path, job.filename, inline)
        return self._error(HTTPStatus.NOT_FOUND, "нет такого адреса")

    def _file(self, path: Path, name: str, inline: bool) -> None:
        """Отдать файл, понимая `Range`.

        Плеер в браузере первым делом спрашивает кусок с конца, чтобы прочитать
        оглавление, и только потом качает подряд. Без `206` он либо не покажет
        перемотку, либо не запустит видео вовсе.
        """
        size = path.stat().st_size
        kind = MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
        disposition = "inline" if inline else "attachment"
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(name, safe='')}",
            "Cache-Control": "private, max-age=600",
        }

        span = self._range(size)
        if span == "bad":
            return self._send(
                HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                b"",
                kind,
                {**headers, "Content-Range": f"bytes */{size}"},
            )

        start, end = span if span else (0, size - 1)
        length = max(0, end - start + 1)
        status = HTTPStatus.PARTIAL_CONTENT if span else HTTPStatus.OK
        if span:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"

        self.send_response(int(status))
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(length))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command == "HEAD" or not length:
            return
        with path.open("rb") as handle, contextlib.suppress(
            BrokenPipeError, ConnectionResetError
        ):
            handle.seek(start)
            left = length
            while left > 0:
                chunk = handle.read(min(CHUNK, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def _range(self, size: int) -> tuple[int, int] | str | None:
        """Разбор заголовка `Range`. None — его нет, "bad" — он бессмысленный.

        Поддерживается один диапазон: этого хватает всем плеерам, а составные
        диапазоны требуют multipart-ответа ради пользы, которой не видно.
        """
        raw = self.headers.get("Range", "").strip()
        if not raw:
            return None
        match = RANGE.fullmatch(raw)
        if not match or size <= 0:
            return "bad"
        first, last = match.group(1), match.group(2)
        if not first and not last:
            return "bad"
        if not first:
            # bytes=-500 — последние 500 байт.
            length = min(int(last), size)
            if length <= 0:
                return "bad"
            return size - length, size - 1
        start = int(first)
        if start >= size:
            return "bad"
        end = min(int(last), size - 1) if last else size - 1
        if end < start:
            return "bad"
        return start, end

    def _events(self, job_id: str) -> None:
        """Поток событий: каждое изменение задачи уходит в браузер сразу."""
        updates: queue.Queue[dict | None] = queue.Queue()
        unsubscribe = self.app.queue.listen(job_id, updates.put)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")  # nginx иначе копит ответ у себя
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        try:
            job = self.app.store.get(job_id)
            if job is not None:
                self._push(job.as_dict())
                if job.status not in ACTIVE:
                    return
            while True:
                try:
                    payload = updates.get(timeout=15.0)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")  # чтобы прокси не закрыл соединение
                    self.wfile.flush()
                    continue
                if payload is None:
                    return
                self._push(payload)
                if payload.get("status") not in ACTIVE:
                    return
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            unsubscribe()

    def _push(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False)
        self.wfile.write(f"data: {body}\n\n".encode())
        self.wfile.flush()

    def _static(self, path: str) -> None:
        if path in ("/", "/index.html"):
            target = WEB / "index.html"
        else:
            candidate = (WEB / path.lstrip("/")).resolve()
            if not str(candidate).startswith(str(WEB.resolve())):
                return self._error(HTTPStatus.FORBIDDEN, "нельзя")
            target = candidate
        if not target.is_file():
            # Сайт — одна страница: любой неизвестный адрес открывает её.
            target = WEB / "index.html"
        body = target.read_bytes()
        kind = STATIC_TYPES.get(
            target.suffix, mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        )
        cache = "no-cache" if target.suffix == ".html" else "public, max-age=3600"
        self._send(HTTPStatus.OK, body, kind, {"Cache-Control": cache})


def serve(
    host: str = "0.0.0.0",
    port: int = 8080,
    data: Path | str = "data",
    *,
    settings: Settings | None = None,
    behind_proxy: bool = False,
) -> None:
    """Запустить сервер и работать, пока не остановят."""
    app = Application(data, settings=settings, behind_proxy=behind_proxy)
    app.start()

    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    print(f"vsaver слушает http://{host}:{port}")
    where = _visible_address(host, port)
    if where:
        print(f"из этой же сети: {where}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nостанавливаюсь…")
    finally:
        server.shutdown()
        app.stop()


def _visible_address(host: str, port: int) -> str:
    """Адрес, по которому сайт видно с соседней машины."""
    if host not in ("0.0.0.0", "::"):
        return f"http://{host}:{port}"
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return f"http://{probe.getsockname()[0]}:{port}"
    except OSError:
        return ""
    finally:
        probe.close()


__all__ = ["Application", "Handler", "serve"]
