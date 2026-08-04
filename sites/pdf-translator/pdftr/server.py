"""HTTP-сервер: приложение на телефоне и API к очереди переводов.

Всё на стандартной библиотеке. Отдельно стоит сказать про две вещи, ради
которых код здесь сложнее, чем «прочитать запрос и ответить»:

* Загрузка идёт на диск потоком. Файл на сто мегабайт нельзя держать в памяти —
  и потому, что памяти жалко, и потому, что на телефоне это единственный способ
  показать честный прогресс отправки.
* Прогресс отдаётся через Server-Sent Events. Это обычный HTTP-ответ, который не
  закрывается, — работает через любой прокси и переживает спящий экран лучше,
  чем веб-сокет, а кода требует втрое меньше.
"""

from __future__ import annotations

import contextlib
import json
import mimetypes
import queue
import re
import shutil
import socket
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from .jobs import Queue, Settings
from .store import ACTIVE, DONE, Store
from .translate.languages import LANGUAGES, TARGETS

WEB = Path(__file__).parent / "web"
MAX_UPLOAD = 300 * 1024 * 1024
CHUNK = 256 * 1024
_SAFE_NAME = re.compile(r"[^\w .()\[\]а-яА-ЯёЁ-]+", re.UNICODE)


def clean_name(name: str) -> str:
    """Имя файла, безопасное и для диска, и для заголовка ответа."""
    name = unquote(name or "").replace("\\", "/").split("/")[-1].strip()
    name = _SAFE_NAME.sub("_", name)[:120]
    if not name.lower().endswith(".pdf"):
        name = (name or "документ") + ".pdf"
    return name


class Application:
    """Сервер целиком: очередь, хранилище и настройки в одном месте."""

    def __init__(
        self,
        data: Path | str = "data",
        *,
        settings: Settings | None = None,
        max_upload: int = MAX_UPLOAD,
    ) -> None:
        self.store = Store(data)
        self.queue = Queue(self.store, settings)
        self.max_upload = max_upload
        self.started = time.time()

    def start(self) -> None:
        self.queue.start()

    def stop(self) -> None:
        self.queue.stop()
        self.store.close()


class Handler(BaseHTTPRequestHandler):
    """Разбор запросов. Экземпляр приложения приходит через класс сервера."""

    protocol_version = "HTTP/1.1"
    server_version = "pdftr"
    sys_version = ""
    app: Application

    # --- служебное ---------------------------------------------------------

    def log_message(self, fmt: str, *args: object) -> None:
        # Стандартный лог печатает каждую картинку — на телефоне это шум.
        if self.path.startswith("/api/") and not self.path.endswith("/events"):
            super().log_message(fmt, *args)

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

    def _json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json({"error": message}, status)

    # --- маршруты ----------------------------------------------------------

    def do_GET(self) -> None:
        url = urlparse(self.path)
        path = url.path
        params = parse_qs(url.query)
        try:
            if path == "/api/config":
                return self._config()
            if path == "/api/jobs":
                return self._json([job.as_dict() for job in self.app.store.recent()])
            match = re.fullmatch(r"/api/jobs/([0-9a-f]{6,32})(/[a-z]+)?", path)
            if match:
                return self._job_route(match.group(1), match.group(2), params)
            return self._static(path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"ошибка сервера: {error}")

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        url = urlparse(self.path)
        try:
            if url.path in ("/api/jobs", "/share"):
                return self._upload(url, share=url.path == "/share")
            return self._error(HTTPStatus.NOT_FOUND, "нет такого адреса")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"ошибка сервера: {error}")

    def do_DELETE(self) -> None:
        match = re.fullmatch(r"/api/jobs/([0-9a-f]{6,32})", urlparse(self.path).path)
        if not match:
            return self._error(HTTPStatus.NOT_FOUND, "нет такого адреса")
        removed = self.app.queue.delete(match.group(1))
        return self._json({"deleted": removed})

    # --- обработчики -------------------------------------------------------

    def _config(self) -> None:
        self._json(
            {
                "languages": LANGUAGES,
                "targets": TARGETS,
                "maxUpload": self.app.max_upload,
                "version": 1,
            }
        )

    def _job_route(self, job_id: str, tail: str | None, params: dict) -> None:
        job = self.app.store.get(job_id)
        if job is None:
            return self._error(HTTPStatus.NOT_FOUND, "задача не найдена")
        if tail is None:
            return self._json(job.as_dict())
        if tail == "/events":
            return self._events(job_id)
        if tail in ("/file", "/source"):
            wanted_result = tail == "/file"
            if wanted_result and job.status != DONE:
                return self._error(HTTPStatus.CONFLICT, "перевод ещё не готов")
            path = (
                self.app.store.result_path(job_id)
                if wanted_result
                else self.app.store.source_path(job_id)
            )
            name = job.name if not wanted_result else _translated_name(job.name, job.target)
            inline = params.get("inline", ["0"])[0] == "1"
            return self._file(path, name, inline)
        return self._error(HTTPStatus.NOT_FOUND, "нет такого адреса")

    def _file(self, path: Path, name: str, inline: bool) -> None:
        if not path.exists():
            return self._error(HTTPStatus.NOT_FOUND, "файл не найден")
        size = path.stat().st_size
        disposition = "inline" if inline else "attachment"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(size))
        self.send_header(
            "Content-Disposition",
            f"{disposition}; filename*=UTF-8''{_percent(name)}",
        )
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as handle, contextlib.suppress(
            BrokenPipeError, ConnectionResetError
        ):
            shutil.copyfileobj(handle, self.wfile, CHUNK)

    def _events(self, job_id: str) -> None:
        """Поток событий: каждое изменение задачи уходит на телефон сразу."""
        updates: queue.Queue[dict | None] = queue.Queue()
        unsubscribe = self.app.queue.listen(job_id, updates.put)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
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

    def _upload(self, url: object, share: bool) -> None:
        params = parse_qs(getattr(url, "query", ""))
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return self._error(HTTPStatus.LENGTH_REQUIRED, "пустой запрос")
        if length > self.app.max_upload:
            # Тело надо прочитать и выбросить, иначе клиент упрётся в закрытое
            # соединение и увидит обрыв связи вместо внятного «файл великоват».
            self._drain(length)
            limit = self.app.max_upload // (1024 * 1024)
            return self._error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                f"файл больше {limit} МБ — столько сервер не принимает",
            )

        temp = self.app.store.files / f"upload-{time.time_ns():x}.part"
        content_type = self.headers.get("Content-Type", "")
        name = clean_name(params.get("name", [""])[0])
        try:
            if content_type.startswith("multipart/form-data"):
                fields, found = _read_multipart(self.rfile, content_type, length, temp)
                name = clean_name(fields.get("filename") or found or name)
                for key in ("source", "target"):
                    if fields.get(key):
                        params[key] = [fields[key]]
            else:
                _read_body(self.rfile, length, temp)
            if not temp.exists() or temp.stat().st_size == 0:
                temp.unlink(missing_ok=True)
                return self._error(HTTPStatus.BAD_REQUEST, "в запросе нет файла")
            if not _looks_like_pdf(temp):
                temp.unlink(missing_ok=True)
                return self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "это не PDF")

            source = (params.get("source", ["auto"])[0] or "auto").strip()
            target = (params.get("target", ["ru"])[0] or "ru").strip()
            job = self.app.queue.submit(name, source, target, temp)
        except OSError as error:
            temp.unlink(missing_ok=True)
            return self._error(HTTPStatus.INSUFFICIENT_STORAGE, f"не удалось сохранить: {error}")

        if share:
            # Телефон отправил файл через «Поделиться» — возвращаем человека в
            # приложение, на карточку только что созданной задачи.
            return self._send(
                HTTPStatus.SEE_OTHER,
                b"",
                "text/plain",
                {"Location": f"/?job={job.id}"},
            )
        return self._json(job.as_dict(), HTTPStatus.ACCEPTED)

    def _drain(self, length: int) -> None:
        """Дочитать тело отвергнутого запроса, но не бесконечно."""
        left = min(length, self.app.max_upload + 8 * 1024 * 1024)
        try:
            while left > 0:
                chunk = self.rfile.read(min(CHUNK, left))
                if not chunk:
                    break
                left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        if left > 0:
            self.close_connection = True  # остаток слишком велик, дальше не ждём

    def _static(self, path: str) -> None:
        if path in ("/", "/index.html"):
            target = WEB / "index.html"
        else:
            candidate = (WEB / path.lstrip("/")).resolve()
            if not str(candidate).startswith(str(WEB.resolve())):
                return self._error(HTTPStatus.FORBIDDEN, "нельзя")
            target = candidate
        if not target.is_file():
            # Приложение — одна страница: любой неизвестный адрес открывает её.
            target = WEB / "index.html"
        body = target.read_bytes()
        kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix in (".html", ".js", ".css", ".webmanifest", ".json", ".svg"):
            kind = {
                ".html": "text/html; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".webmanifest": "application/manifest+json; charset=utf-8",
                ".json": "application/json; charset=utf-8",
                ".svg": "image/svg+xml",
            }[target.suffix]
        cache = "no-cache" if target.suffix == ".html" else "public, max-age=3600"
        self._send(HTTPStatus.OK, body, kind, {"Cache-Control": cache})


def _percent(name: str) -> str:
    return quote(name, safe="")


def _translated_name(name: str, target: str) -> str:
    stem = name[:-4] if name.lower().endswith(".pdf") else name
    return f"{stem} ({target}).pdf"


def _looks_like_pdf(path: Path) -> bool:
    with path.open("rb") as handle:
        return b"%PDF" in handle.read(2048)


def _read_body(source: object, length: int, target: Path) -> None:
    """Тело запроса на диск кусками — в память не берём ничего."""
    left = length
    with target.open("wb") as out:
        while left > 0:
            chunk = source.read(min(CHUNK, left))
            if not chunk:
                break
            out.write(chunk)
            left -= len(chunk)


def _read_multipart(
    source: object, content_type: str, length: int, target: Path
) -> tuple[dict[str, str], str]:
    """Разобрать multipart, вытащив файл на диск, а поля — в словарь.

    Своя реализация вместо `cgi`: тот модуль удалён из Python 3.13 и всё равно
    читает тело целиком в память, чего с файлом на сотню мегабайт делать нельзя.
    Здесь же данные идут через буфер в пару сотен килобайт, а на диск попадает
    только содержимое файловой части.
    """
    match = re.search(r'boundary="?([^";]+)"?', content_type)
    if not match:
        raise ValueError("в multipart нет границы частей")
    boundary = b"--" + match.group(1).encode("latin-1")
    body = _Body(source, length)
    fields: dict[str, str] = {}
    filename = ""

    if not body.read_until(boundary, lambda _chunk: None):
        raise ValueError("multipart без единой границы")

    while body.ensure(2):
        lead = bytes(body.buffer[:2])
        if lead == b"--":
            break  # закрывающая граница
        if lead == b"\r\n":
            del body.buffer[:2]

        headers = bytearray()
        if not body.read_until(b"\r\n\r\n", headers.extend):
            break
        name, part_name = _part_names(bytes(headers))

        if part_name:
            filename = part_name
            with target.open("wb") as out:
                found = body.read_until(b"\r\n" + boundary, out.write)
        else:
            value = bytearray()
            found = body.read_until(b"\r\n" + boundary, value.extend)
            if name:
                fields[name] = value.decode("utf-8", "replace").strip()
        if not found:
            break
    return fields, filename


def _part_names(headers: bytes) -> tuple[str, str]:
    """Имя поля и имя файла из заголовков одной части."""
    text = headers.decode("utf-8", "replace")
    name = re.search(r'name="([^"]*)"', text)
    plain = re.search(r'filename="([^"]*)"', text)
    encoded = re.search(r"filename\*=(?:UTF-8'')?([^;\r\n]+)", text)
    found = ""
    if plain and plain.group(1):
        found = plain.group(1)
    elif encoded and encoded.group(1):
        found = unquote(encoded.group(1).strip('"'))
    return (name.group(1) if name else ""), found


class _Body:
    """Тело запроса кусками, с поиском границы через скользящий буфер."""

    def __init__(self, source: object, length: int) -> None:
        self.source = source
        self.left = length
        self.buffer = bytearray()

    def more(self) -> bool:
        """Дочитать очередной кусок. False — тело кончилось."""
        if self.left <= 0:
            return False
        data = self.source.read(min(CHUNK, self.left))
        if not data:
            self.left = 0
            return False
        self.left -= len(data)
        self.buffer += data
        return True

    def ensure(self, size: int) -> bool:
        while len(self.buffer) < size:
            if not self.more():
                return len(self.buffer) >= size
        return True

    def read_until(self, marker: bytes, sink) -> bool:
        """Отдать в `sink` всё до маркера и съесть его. False — не нашёлся.

        В буфере всегда придерживаются последние байты длиной с маркер: иначе
        граница, попавшая на стык кусков, была бы пропущена.
        """
        keep = len(marker) - 1
        while True:
            index = self.buffer.find(marker)
            if index >= 0:
                sink(bytes(self.buffer[:index]))
                del self.buffer[: index + len(marker)]
                return True
            if len(self.buffer) > keep:
                edge = len(self.buffer) - keep
                sink(bytes(self.buffer[:edge]))
                del self.buffer[:edge]
            if not self.more():
                sink(bytes(self.buffer))
                self.buffer.clear()
                return False


def serve(
    host: str = "0.0.0.0",
    port: int = 8080,
    data: Path | str = "data",
    *,
    settings: Settings | None = None,
    max_upload: int = MAX_UPLOAD,
) -> None:
    """Запустить сервер и работать, пока не остановят."""
    app = Application(data, settings=settings, max_upload=max_upload)
    app.start()

    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    where = _visible_address(host, port)
    print(f"pdftr слушает http://{host}:{port}")
    if where:
        print(f"с телефона в той же сети: {where}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nостанавливаюсь…")
    finally:
        server.shutdown()
        app.stop()


def _visible_address(host: str, port: int) -> str:
    """Адрес, который стоит набрать на телефоне: наш IP в локальной сети."""
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
