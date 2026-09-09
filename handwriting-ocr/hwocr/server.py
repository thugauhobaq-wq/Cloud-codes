"""HTTP-сервер: приложение на телефоне и API к очереди распознавания.

Всё на стандартной библиотеке. Каркас — приём тела на диск кусками, разбор
multipart, поток событий, отдача статики — перенесён из
`pdf-translator/pdftr/server.py` как есть: там он уже проверен на больших
файлах и «Поделиться» с Android. Своё здесь — проверка, что прислали именно
фото, отдача снимка для превью и сохранение правок текста.
"""

from __future__ import annotations

import contextlib
import json
import mimetypes
import queue
import re
import socket
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from .jobs import Queue, Settings
from .recognize import MEDIA_TYPES, Recognizer, sniff_image
from .store import ACTIVE, DONE, Store

WEB = Path(__file__).parent / "web"
MAX_UPLOAD = 15 * 1024 * 1024
MAX_TEXT = 1024 * 1024
CHUNK = 256 * 1024
_SAFE_NAME = re.compile(r"[^\w .()\[\]а-яА-ЯёЁ-]+", re.UNICODE)
_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def clean_name(name: str, media_type: str = "image/jpeg") -> str:
    """Имя файла, безопасное для диска и заголовков, с расширением по типу."""
    name = unquote(name or "").replace("\\", "/").split("/")[-1].strip()
    name = _SAFE_NAME.sub("_", name)[:120]
    extension = _EXTENSIONS.get(media_type, ".jpg")
    stem = re.sub(r"\.(jpe?g|png|webp|heic|heif)$", "", name, flags=re.IGNORECASE)
    return (stem or "фото") + extension


class Application:
    """Сервер целиком: очередь, хранилище и настройки в одном месте."""

    def __init__(
        self,
        data: Path | str = "data",
        *,
        settings: Settings | None = None,
        max_upload: int = MAX_UPLOAD,
        recognizer: Recognizer | None = None,
    ) -> None:
        self.store = Store(data)
        self.queue = Queue(self.store, settings, recognizer)
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
    server_version = "hwocr"
    sys_version = ""
    app: Application

    # --- служебное ---------------------------------------------------------

    def log_message(self, fmt: str, *args: object) -> None:
        # Стандартный лог печатает каждую иконку — на телефоне это шум.
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
        try:
            if path == "/api/config":
                return self._config()
            if path == "/api/jobs":
                return self._json([job.as_dict() for job in self.app.store.recent()])
            match = re.fullmatch(r"/api/jobs/([0-9a-f]{6,32})(/[a-z]+)?", path)
            if match:
                return self._job_route(match.group(1), match.group(2))
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

    def do_PUT(self) -> None:
        match = re.fullmatch(r"/api/jobs/([0-9a-f]{6,32})/text", urlparse(self.path).path)
        try:
            if not match:
                return self._error(HTTPStatus.NOT_FOUND, "нет такого адреса")
            return self._put_text(match.group(1))
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
        recognizer = self.app.queue.recognizer
        self._json(
            {
                "engine": recognizer.name,
                "model": getattr(recognizer, "model", ""),
                "maxUpload": self.app.max_upload,
                "keepHours": self.app.queue.settings.keep_hours,
                "accept": sorted(MEDIA_TYPES),
                "version": 1,
            }
        )

    def _job_route(self, job_id: str, tail: str | None) -> None:
        job = self.app.store.get(job_id)
        if job is None:
            return self._error(HTTPStatus.NOT_FOUND, "задача не найдена")
        if tail is None:
            return self._json(job.as_dict())
        if tail == "/events":
            return self._events(job_id)
        if tail == "/image":
            return self._image(job_id, job.media_type)
        return self._error(HTTPStatus.NOT_FOUND, "нет такого адреса")

    def _image(self, job_id: str, media_type: str) -> None:
        path = self.app.store.image_path(job_id)
        if not path.exists():
            return self._error(HTTPStatus.NOT_FOUND, "фото уже удалено")
        body = path.read_bytes()
        # Фото не меняется, но чужим кэшам его отдавать не надо: на нём может
        # быть что угодно. `private` — только браузеру этого телефона.
        self._send(
            HTTPStatus.OK,
            body,
            media_type or "application/octet-stream",
            {"Cache-Control": "private, max-age=86400", "Content-Disposition": "inline"},
        )

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

    def _put_text(self, job_id: str) -> None:
        job = self.app.store.get(job_id)
        if job is None:
            return self._error(HTTPStatus.NOT_FOUND, "задача не найдена")
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_TEXT:
            self._drain(length)
            return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "текст слишком длинный")
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(raw.decode("utf-8"))
            text = payload["text"]
            if not isinstance(text, str):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            return self._error(HTTPStatus.BAD_REQUEST, "ожидается JSON вида {\"text\": \"...\"}")
        if job.status != DONE:
            return self._error(HTTPStatus.CONFLICT, "текст ещё не распознан")
        self.app.store.set_text(job_id, text, edited=True)
        return self._json(self.app.store.get(job_id).as_dict())

    def _upload(self, url: object, share: bool) -> None:
        params = parse_qs(getattr(url, "query", ""))
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return self._reject(HTTPStatus.LENGTH_REQUIRED, "пустой запрос", share)
        if length > self.app.max_upload:
            # Тело надо прочитать и выбросить, иначе клиент упрётся в закрытое
            # соединение и увидит обрыв связи вместо внятного «файл великоват».
            self._drain(length)
            limit = self.app.max_upload // (1024 * 1024)
            return self._reject(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                f"фото больше {limit} МБ — столько сервер не принимает",
                share,
            )

        temp = self.app.store.files / f"upload-{time.time_ns():x}.part"
        content_type = self.headers.get("Content-Type", "")
        name = params.get("name", [""])[0]
        try:
            if content_type.startswith("multipart/form-data"):
                fields, found = _read_multipart(self.rfile, content_type, length, temp)
                name = fields.get("filename") or found or fields.get("title") or name
            else:
                _read_body(self.rfile, length, temp)
            if not temp.exists() or temp.stat().st_size == 0:
                temp.unlink(missing_ok=True)
                return self._reject(HTTPStatus.BAD_REQUEST, "в запросе нет файла", share)

            with temp.open("rb") as handle:
                media_type = sniff_image(handle.read(16))
            if media_type == "image/heic":
                temp.unlink(missing_ok=True)
                return self._reject(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    "это HEIC с iPhone — откройте приложение и выберите фото в нём, "
                    "оно само переведёт снимок в JPEG",
                    share,
                )
            if media_type not in MEDIA_TYPES:
                temp.unlink(missing_ok=True)
                return self._reject(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    "это не фотография: принимаются JPEG, PNG и WebP",
                    share,
                )
            job = self.app.queue.submit(clean_name(name, media_type), media_type, temp)
        except OSError as error:
            temp.unlink(missing_ok=True)
            return self._reject(
                HTTPStatus.INSUFFICIENT_STORAGE, f"не удалось сохранить: {error}", share
            )

        if share:
            # Телефон отправил фото через «Поделиться» — возвращаем человека в
            # приложение, на карточку только что созданной задачи.
            return self._send(
                HTTPStatus.SEE_OTHER, b"", "text/plain", {"Location": f"/?job={job.id}"}
            )
        return self._json(job.as_dict(), HTTPStatus.ACCEPTED)

    def _reject(self, status: HTTPStatus, message: str, share: bool) -> None:
        """Отказ: JSON для приложения, а из «Поделиться» — обратно на экран с текстом."""
        if share:
            return self._send(
                HTTPStatus.SEE_OTHER,
                b"",
                "text/plain",
                {"Location": f"/?error={quote(message)}"},
            )
        return self._error(status, message)

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
    читает тело целиком в память. Здесь данные идут через буфер в пару сотен
    килобайт, а на диск попадает только содержимое файловой части. Если файлов
    несколько, остаётся последний: «Поделиться» одним фото — обычный случай.
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
    recognizer: Recognizer | None = None,
) -> None:
    """Запустить сервер и работать, пока не остановят."""
    app = Application(data, settings=settings, max_upload=max_upload, recognizer=recognizer)
    app.start()

    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    where = _visible_address(host, port)
    print(f"hwocr слушает http://{host}:{port}, движок: {app.queue.recognizer.name}")
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


__all__ = ["Application", "Handler", "clean_name", "serve"]
