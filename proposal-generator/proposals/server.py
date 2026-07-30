"""Веб-форма и страница предложения для заказчика.

Обычный `http.server` из стандартной библиотеки. Маршруты делятся на две части,
и это главное решение в модуле:

* **приватные** — форма, черновики, отправка. Доступны только с самой машины;
  если сервер поднят на внешнем адресе, к ним дополнительно нужен ключ.
* **публичные** — `/p/<токен>`: страница предложения для заказчика. Открыта
  всем, кто знает ссылку из письма, и не даёт добраться ни до чужих
  предложений, ни до формы.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import secrets
import webbrowser
from decimal import Decimal
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import delivery as delivery_module
from . import invoice as invoice_module
from . import mailer, storage
from .delivery import Delivery
from .mailer import MailError, SmtpConfig
from .money import format_amount
from .pdf import FontError, find_family
from .pdf.fonts import FontFamily
from .sample import demo_proposal
from .storage import Drafts, Record, StorageError
from .template import TEMPLATES, format_date, render

WEB_ROOT = Path(__file__).resolve().parent / "web"
"""Статика лежит внутри пакета — тогда она едет и в колесо, и в editable-установку."""
MAX_BODY = 12 * 1024 * 1024
"""Логотипы приезжают внутри JSON в base64 — с запасом на пару крупных PNG."""

PUBLIC_PREFIX = "/p/"
ADMIN_COOKIE = "proposals_admin"

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

    def __init__(
        self,
        *args,
        drafts: Drafts,
        family: FontFamily | None,
        public_url: str = "",
        admin_token: str = "",
        **kwargs,
    ) -> None:
        self.drafts = drafts
        self.family = family
        self.public_url = public_url.rstrip("/")
        self.admin_token = admin_token
        self._set_admin_cookie = False
        super().__init__(*args, **kwargs)

    # --- маршруты ------------------------------------------------------------

    def do_GET(self) -> None:
        path = self._path()
        if path.startswith(PUBLIC_PREFIX):
            self._public_get(path)
            return
        if not self._admin_allowed():
            return

        match path:
            case "/" | "/index.html":
                self._static("index.html")
            case "/api/state":
                self._send_json(self._state())
            case "/api/demo":
                self._send_json(storage.to_json(demo_proposal()))
            case "/api/drafts":
                self._send_json({"drafts": [info.to_dict() for info in self.drafts.list()]})
            case "/api/funnel":
                self._send_json(self._funnel())
            case "/api/smtp":
                self._send_json(self._smtp_config().safe_dict())
            case _ if path.startswith("/api/drafts/"):
                self._read_draft(path.rsplit("/", 1)[-1])
            case _:
                self._static(path.lstrip("/"))

    def do_POST(self) -> None:
        path = self._path()
        if path.startswith(PUBLIC_PREFIX):
            self._public_post(path)
            return
        if not self._admin_allowed():
            return

        try:
            payload = self._read_json()
        except ValueError as error:
            self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return

        match path:
            case "/api/render":
                self._render(payload)
            case "/api/invoice":
                self._invoice(payload)
            case "/api/send":
                self._send_proposal(payload)
            case "/api/remind":
                self._remind(payload)
            case "/api/smtp":
                self._save_smtp(payload)
            case _ if path.startswith("/api/drafts/"):
                self._write_draft(path.rsplit("/", 1)[-1], payload)
            case _:
                self._send_json({"error": "нет такого метода"}, HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:
        path = self._path()
        if not self._admin_allowed():
            return
        if not path.startswith("/api/drafts/"):
            self._send_json({"error": "нет такого метода"}, HTTPStatus.NOT_FOUND)
            return
        try:
            removed = self.drafts.delete(path.rsplit("/", 1)[-1])
        except StorageError as error:
            self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"deleted": removed})

    # --- доступ к приватной части --------------------------------------------

    def _admin_allowed(self) -> bool:
        """Пускать ли к форме. При отказе ответ уже отправлен."""
        if self._from_localhost() or not self.admin_token:
            return True
        if secrets.compare_digest(self._supplied_key(), self.admin_token):
            self._set_admin_cookie = True
            return True
        self._send_json(
            {"error": "нужен ключ доступа: откройте ссылку, показанную при запуске"},
            HTTPStatus.FORBIDDEN,
        )
        return False

    def _from_localhost(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except (ValueError, IndexError):
            return False

    def _supplied_key(self) -> str:
        """Ключ из строки запроса или из ранее поставленной cookie."""
        query = parse_qs(urlsplit(self.path).query)
        if query.get("key"):
            return query["key"][0]
        for chunk in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = chunk.strip().partition("=")
            if name == ADMIN_COOKIE:
                return unquote(value)
        return ""

    # --- публичная страница предложения --------------------------------------

    def _public_get(self, path: str) -> None:
        token, _, tail = path[len(PUBLIC_PREFIX) :].partition("/")
        found = self.drafts.find_by_token(token)
        if found is None:
            self._not_found_page()
            return
        slug, record = found

        match tail:
            case "":
                self._static("view.html")
            case "data":
                # Отметку о просмотре ставим здесь, а не на выдаче страницы:
                # ссылку могут дёрнуть предпросмотрщики почты, а данные
                # запрашивает уже открытая в браузере страница.
                record = self.drafts.update(slug, lambda item: item.delivery.mark_viewed())
                self._send_json(self._public_data(record))
            case "pdf":
                self._public_pdf(record)
            case _:
                self._not_found_page()

    def _public_post(self, path: str) -> None:
        token, _, tail = path[len(PUBLIC_PREFIX) :].partition("/")
        if tail != "decision":
            self._send_json({"error": "нет такого метода"}, HTTPStatus.NOT_FOUND)
            return
        found = self.drafts.find_by_token(token)
        if found is None:
            self._send_json({"error": "ссылка не найдена"}, HTTPStatus.NOT_FOUND)
            return
        slug, _ = found

        try:
            payload = self._read_json()
        except ValueError as error:
            self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return

        accepted = bool(payload.get("accept"))
        comment = str(payload.get("comment") or "")[:2000]
        record = self.drafts.update(slug, lambda item: item.delivery.decide(accepted, comment))
        self._send_json(record.delivery.public_dict())

    def _public_data(self, record: Record) -> dict:
        """Что видит заказчик. Ни токена, ни следов других предложений."""
        proposal = record.proposal
        return {
            "number": proposal.number,
            "issued_at": format_date(proposal.issued_at),
            "valid_until": format_date(proposal.valid_until) if proposal.valid_days else "",
            "subject": proposal.subject,
            "sender": proposal.sender.name,
            "sender_contacts": proposal.sender.contacts,
            "client": proposal.client.name,
            "total": format_amount(proposal.totals().total, proposal.currency),
            "vat_note": proposal.vat_note,
            "items": [
                {"title": item.title, "total": format_amount(item.total, proposal.currency)}
                for item in proposal.items
            ],
            "accent": proposal.accent,
            "delivery": record.delivery.public_dict(),
        }

    def _public_pdf(self, record: Record) -> None:
        try:
            data, _ = render(record.proposal, family=self.family)
        except (FontError, ValueError) as error:
            self._send_json({"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'inline; filename="{record.proposal.filename}"')
        self.end_headers()
        self.wfile.write(data)

    def _not_found_page(self) -> None:
        body = (
            "<!doctype html><meta charset=utf-8><title>Ссылка не найдена</title>"
            "<div style='font:16px/1.5 system-ui;max-width:520px;margin:15vh auto;"
            "padding:0 20px;color:#16181d'>"
            "<h1 style='font-size:22px'>Ссылка не действует</h1>"
            "<p style='color:#6b7280'>Возможно, предложение отозвали или адрес "
            "скопирован не целиком. Напишите отправителю — он пришлёт новую.</p></div>"
        ).encode()
        self.send_response(HTTPStatus.NOT_FOUND)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- приватные действия --------------------------------------------------

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
            "public_url": self.public_url,
            "smtp": self._smtp_config().safe_dict(),
        }

    def _funnel(self) -> dict:
        items = []
        for info in self.drafts.list():
            try:
                record = storage.load(info.path)
            except StorageError:
                continue
            items.append(
                (record.delivery.status, record.proposal.totals().total, record.proposal.currency)
            )
        summary = delivery_module.summarize(items)
        summary["money"] = {
            currency: {
                key: format_amount(Decimal(value), currency) for key, value in bucket.items()
            }
            for currency, bucket in summary["money"].items()
        }
        return summary

    def _render(self, payload: dict) -> None:
        record = storage.from_json(payload)
        try:
            data, warnings = render(record.proposal, family=self.family)
        except FontError as error:
            self._send_json({"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        except ValueError as error:  # битая картинка, странный цвет
            self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'inline; filename="{record.proposal.filename}"')
        if warnings:
            # Заголовок, а не тело: тело здесь — сам PDF.
            self.send_header("X-Proposal-Warnings", quote("; ".join(warnings)))
        self.end_headers()
        self.wfile.write(data)

    def _invoice(self, payload: dict) -> None:
        """Счёт по тем же данным. Берём сохранённое, если черновик известен."""
        slug = str(payload.get("slug") or "").strip()
        if slug:
            try:
                proposal = self.drafts.read(slug).proposal
            except StorageError:
                self._send_json({"error": "черновик не найден"}, HTTPStatus.NOT_FOUND)
                return
        else:
            proposal = storage.from_json(payload).proposal

        try:
            data, warnings = invoice_module.render(
                proposal,
                number=str(payload.get("number") or ""),
                family=self.family,
            )
        except (FontError, ValueError) as error:
            self._send_json({"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        name = proposal.filename.replace("KP-", "Schet-", 1)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'inline; filename="{name}"')
        if warnings:
            self.send_header("X-Proposal-Warnings", quote("; ".join(warnings)))
        self.end_headers()
        self.wfile.write(data)

    def _remind(self, payload: dict) -> None:
        slug = str(payload.get("slug") or "").strip()
        if not slug:
            self._send_json({"error": "не указан черновик"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            record = self.drafts.read(slug)
        except StorageError:
            self._send_json({"error": "черновик не найден"}, HTTPStatus.NOT_FOUND)
            return

        try:
            pdf, _ = render(record.proposal, family=self.family)
        except (FontError, ValueError) as error:
            self._send_json({"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        dry_run = bool(payload.get("dry_run"))
        try:
            message = mailer.remind(
                self._smtp_config(),
                record.proposal,
                record.delivery,
                pdf,
                base_url=self.public_url,
                subject=str(payload.get("subject") or ""),
                body=str(payload.get("body") or ""),
                dry_run=dry_run,
            )
        except MailError as error:
            self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return

        if not dry_run:
            self.drafts.write(slug, record)
        self._send_json(
            {
                "dry_run": dry_run,
                "to": message["To"],
                "subject": message["Subject"],
                "body": message.get_body(("plain",)).get_content(),
                "reminders": record.delivery.reminders,
            }
        )

    def _send_proposal(self, payload: dict) -> None:
        """Отправить сохранённый черновик письмом.

        Отправляем именно сохранённое, а не то, что сейчас в форме: иначе в
        журнале доставки окажется одна версия предложения, а у заказчика —
        другая.
        """
        slug = str(payload.get("slug") or "").strip()
        if not slug:
            self._send_json(
                {"error": "сначала сохраните черновик — отправляем сохранённое"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        try:
            record = self.drafts.read(slug)
        except StorageError:
            self._send_json({"error": "черновик не найден"}, HTTPStatus.NOT_FOUND)
            return

        try:
            pdf, warnings = render(record.proposal, family=self.family)
        except (FontError, ValueError) as error:
            self._send_json({"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        dry_run = bool(payload.get("dry_run"))
        try:
            message = mailer.deliver(
                self._smtp_config(),
                record.proposal,
                record.delivery,
                pdf,
                to=str(payload.get("to") or ""),
                base_url=self.public_url,
                subject=str(payload.get("subject") or ""),
                body=str(payload.get("body") or ""),
                dry_run=dry_run,
            )
        except MailError as error:
            self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return

        if not dry_run:
            self.drafts.write(slug, record)
        if not self.public_url:
            warnings = [
                *warnings,
                "публичный адрес не задан: в письме нет ссылки на предложение, "
                "поэтому «просмотрено» и «принято» отмечаться не будут — "
                "запустите сервер с --public-url",
            ]

        self._send_json(
            {
                "dry_run": dry_run,
                "status": record.delivery.status,
                "status_label": record.delivery.label,
                "link": record.delivery.link(self.public_url) if self.public_url else "",
                "subject": message["Subject"],
                "to": message["To"],
                "body": message.get_body(("plain",)).get_content(),
                "warnings": warnings,
            }
        )

    def _smtp_config(self) -> SmtpConfig:
        try:
            return mailer.load_config(self.drafts.directory)
        except MailError:
            return SmtpConfig()

    def _save_smtp(self, payload: dict) -> None:
        current = self._smtp_config()
        raw = dict(payload)
        # Пустой пароль в форме означает «оставить прежний», а не «стереть»:
        # обратно из файла он никогда не показывается.
        if not str(raw.get("password") or ""):
            raw["password"] = current.password
        config = SmtpConfig.from_dict(raw)
        try:
            path = mailer.save_config(config, self.drafts.directory)
        except OSError as error:
            self._send_json({"error": f"не сохранилось: {error}"}, HTTPStatus.BAD_REQUEST)
            return

        answer = {"saved": str(path), **config.safe_dict()}
        if payload.get("check"):
            try:
                answer["check"] = mailer.check(config)
            except MailError as error:
                answer["check_error"] = str(error)
        self._send_json(answer)

    def _read_draft(self, slug: str) -> None:
        try:
            record = self.drafts.read(slug)
        except StorageError:
            self._send_json({"error": "черновик не найден"}, HTTPStatus.NOT_FOUND)
            return
        raw = storage.to_json(record)
        raw["delivery"]["link"] = record.delivery.link(self.public_url) if self.public_url else ""
        raw["delivery"]["events"] = [
            {**event.to_dict(), "label": event.label} for event in record.delivery.events
        ]
        raw["delivery"]["status_label"] = record.delivery.label
        raw["delivery"]["silent_days"] = record.delivery.silent_days()
        raw["delivery"].pop("token", None)  # форме токен не нужен
        self._send_json(raw)

    def _write_draft(self, slug: str, payload: dict) -> None:
        incoming = storage.from_json(payload)
        try:
            existing: Delivery | None = self.drafts.read(slug).delivery
        except StorageError:
            existing = None
        # Доставка принадлежит серверу, а не форме: иначе достаточно было бы
        # сохранить черновик, чтобы заодно переписать статус и токен.
        record = Record(proposal=incoming.proposal, delivery=existing or Delivery())

        try:
            path = self.drafts.write(slug, record)
        except (StorageError, OSError) as error:
            self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json(
            {"saved": slug, "path": str(path), "delivery": record.delivery.public_dict()}
        )

    # --- служебное -----------------------------------------------------------

    def _path(self) -> str:
        """Путь запроса без строки параметров и с развёрнутыми %XX.

        Имя черновика браузер шлёт через `encodeURIComponent`, так что без
        `unquote` кириллический слаг доехал бы сюда набором процентов.
        """
        return unquote(urlsplit(self.path).path)

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
        self._maybe_set_cookie()
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
        self._maybe_set_cookie()
        self.end_headers()
        self.wfile.write(body)

    def _maybe_set_cookie(self) -> None:
        """Запомнить ключ доступа, чтобы он был нужен только в первой ссылке."""
        if self._set_admin_cookie:
            self.send_header(
                "Set-Cookie",
                f"{ADMIN_COOKIE}={quote(self.admin_token)}; Path=/; HttpOnly; SameSite=Strict",
            )
            self._set_admin_cookie = False

    def log_message(self, format: str, *args) -> None:
        # Стандартный лог печатает каждую картинку и каждый опрос — шумно.
        if args and str(args[0]).startswith(
            ("POST /api/render", "POST /api/send", "GET /api/state", "GET /p/", "POST /p/")
        ):
            super().log_message(format, *args)


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost", "")


def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    data_dir: str | Path = storage.DEFAULT_DIR,
    font: str | None = None,
    public_url: str = "",
    admin_token: str = "",
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

    external = not is_loopback(host)
    if external and not admin_token:
        # Наружу без ключа форму выставлять нельзя: она умеет отправлять почту
        # и отдаёт все черновики. Ключ придумываем сами, чтобы это нельзя было
        # пропустить по невнимательности.
        admin_token = secrets.token_urlsafe(24)

    handler = partial(
        Handler, drafts=drafts, family=family, public_url=public_url, admin_token=admin_token
    )
    server = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}"
    entry = f"{url}/?key={quote(admin_token)}" if admin_token else url

    print(f"Форма: {entry}")
    print(f"Черновики: {drafts.directory}")
    if family:
        print(f"Шрифт: {family.name}")
    if public_url:
        print(f"Ссылки для заказчиков: {public_url.rstrip('/')}/p/…")
    else:
        print(
            "Публичный адрес не задан: ссылки в письмах и статусы «просмотрено»\n"
            "и «принято» работать не будут. Задайте --public-url."
        )
    if external:
        print("Форма открыта наружу — доступ к ней только по ссылке с ключом.")

    if open_browser:
        # Без графической оболочки открывать нечего — это не ошибка.
        with contextlib.suppress(Exception):
            webbrowser.open(entry)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        server.server_close()
