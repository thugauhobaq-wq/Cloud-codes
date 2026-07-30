"""HTTP-сервер: раздаёт виджет, принимает отзывы, отдаёт админку.

Всё на stdlib. Три группы маршрутов:

* публичные для чужих сайтов — `/w/<key>.js`, `/embed.js`, `/api/v1/*`;
* страницы — форма `/f/<key>`, превью `/preview/<key>`, витрина `/`;
* админские `/api/admin/*` — по токену сайта.
"""

from __future__ import annotations

import json
import mimetypes
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import dashboard, telegram
from .dashboard import DashboardError
from .models import (
    MAX_REPLY,
    Review,
    Site,
    ValidationError,
    clean_text,
    hash_token,
    normalize_domains,
    now_iso,
)
from .moderation import RateLimiter, check_submission, check_text, decide_status
from .storage import DEFAULT_DB, Storage

ASSETS = Path(__file__).resolve().parent / "assets"
MAX_BODY = 32 * 1024
WIDGET_CACHE = "public, max-age=300"
API_CACHE = "public, max-age=60"

_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")


class WidgetHandler(BaseHTTPRequestHandler):
    server_version = "ReviewsWidget/1.0"
    protocol_version = "HTTP/1.1"

    db_path: str = str(DEFAULT_DB)
    public_url: str = ""
    #: Путь к базе reviews-dashboard. Пусто — импорт с площадок выключен.
    dashboard_db: str = ""
    limiter = RateLimiter()

    # ------------------------------------------------------------- сервис

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def _storage(self) -> Storage:
        return Storage(self.db_path)

    def _send(
        self,
        body: bytes,
        content_type: str = "text/html; charset=utf-8",
        status: int = 200,
        cache: str = "no-store",
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200, cache: str = "no-store",
              extra: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", status, cache, extra)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > MAX_BODY:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Форма может уйти и как application/x-www-form-urlencoded.
            data = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8", "replace")).items()}
        return data if isinstance(data, dict) else {}

    def _origin(self) -> str:
        return self.headers.get("Origin") or ""

    def _same_host(self, origin: str) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        origin_host = re.sub(r"^https?://", "", origin).split("/")[0].split(":")[0].lower()
        return bool(host) and host == origin_host

    def _client_ip(self) -> str:
        forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        return forwarded or self.client_address[0]

    def _asset(self, name: str) -> bytes:
        path = (ASSETS / name).resolve()
        if not str(path).startswith(str(ASSETS)) or not path.is_file():
            raise FileNotFoundError(name)
        return path.read_bytes()

    def _page(self, name: str, replacements: dict[str, str] | None = None) -> str:
        text = self._asset(name).decode("utf-8")
        for token, value in (replacements or {}).items():
            text = text.replace(token, value)
        return text

    def _base_url(self) -> str:
        if self.public_url:
            return self.public_url.rstrip("/")
        return f"http://{self.headers.get('Host') or 'localhost'}"

    # ------------------------------------------------------------ маршруты

    def do_GET(self) -> None:  # noqa: N802 — имя задаёт BaseHTTPRequestHandler
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

        try:
            if route == "/":
                self._landing()
            elif route == "/embed.js":
                self._send(self._asset("widget.js"), "application/javascript; charset=utf-8",
                           cache=WIDGET_CACHE)
            elif route.startswith("/w/") and route.endswith(".js"):
                self._widget_script(route[3:-3], params)
            elif route == "/api/v1/widget":
                self._widget_data(params)
            elif route.startswith("/f/"):
                self._form_page(route[3:], params)
            elif route.startswith("/preview/"):
                self._preview_page(route[9:])
            elif route == "/admin":
                self._send(self._asset("admin.html"))
            elif route == "/api/admin/site":
                self._admin_site()
            elif route == "/api/admin/reviews":
                self._admin_reviews(params)
            elif route == "/api/admin/platforms":
                self._admin_platforms()
            elif route.startswith("/assets/"):
                self._static(route[8:])
            elif route == "/healthz":
                self._json({"ok": True, "time": now_iso()})
            elif route == "/favicon.ico":
                # Иконки нет, но и 404 в консоли чужого сайта видеть незачем.
                self._send(b"", "image/x-icon", status=204, cache=WIDGET_CACHE)
            else:
                self._json({"error": "не найдено"}, status=404)
        except FileNotFoundError:
            self._json({"error": "не найдено"}, status=404)
        except PermissionError as error:
            self._json({"error": str(error)}, status=403)
        except Exception as error:  # noqa: BLE001 — сервер не должен падать из-за одного запроса
            self.log_message("ошибка: %s", error)
            self._json({"error": "внутренняя ошибка"}, status=500)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        try:
            if route == "/api/v1/reviews":
                self._submit()
            elif route == "/api/admin/site":
                self._admin_update()
            elif route.startswith("/api/admin/reviews/"):
                self._admin_action(route.rsplit("/", 1)[-1])
            elif route == "/api/admin/platforms":
                self._admin_pull()
            else:
                self._json({"error": "не найдено"}, status=404)
        except PermissionError as error:
            self._json({"error": str(error)}, status=403)
        except ValidationError as error:
            self._json({"error": str(error)}, status=400)
        except Exception as error:  # noqa: BLE001
            self.log_message("ошибка: %s", error)
            self._json({"error": "внутренняя ошибка"}, status=500)

    def do_OPTIONS(self) -> None:  # noqa: N802
        """Предполётный запрос для формы, встроенной на чужой домен."""
        origin = self._origin()
        headers = {
            "Access-Control-Allow-Origin": origin or "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "86400",
        }
        self._send(b"", "text/plain", status=204, extra=headers)

    # ------------------------------------------------------ публичная часть

    def _site_or_404(self, storage: Storage, key: str) -> Site:
        if not _KEY_RE.match(key or ""):
            raise FileNotFoundError(key)
        site = storage.get_site(key)
        if site is None:
            raise FileNotFoundError(key)
        return site

    def _widget_script(self, key: str, params: dict[str, str] | None = None) -> None:
        """`/w/<key>.js` — виджет с зашитым ключом и настройками сайта.

        `?preview=1` отключает кеш: в кабинете правки настроек должны быть
        видны сразу, а не через пять минут.
        """
        preview = (params or {}).get("preview") == "1"
        with self._storage() as storage:
            try:
                site = self._site_or_404(storage, key)
            except FileNotFoundError:
                self._send("/* reviews-widget: сайт не найден */".encode("utf-8"),
                           "application/javascript; charset=utf-8", status=404)
                return
            config: dict[str, Any] = {
                "site": site.key,
                "name": site.name,
                "settings": site.settings,
            }
            if self.public_url:
                config["api"] = self.public_url.rstrip("/")
            if preview:
                config["nocache"] = True

        prefix = "window.__REVIEWS_WIDGET__=" + json.dumps(config, ensure_ascii=False) + ";\n"
        body = prefix.encode("utf-8") + self._asset("widget.js")
        self._send(body, "application/javascript; charset=utf-8",
                   cache="no-store" if preview else WIDGET_CACHE,
                   extra={"Access-Control-Allow-Origin": "*"})

    def _widget_data(self, params: dict[str, str]) -> None:
        """Данные для виджета. Отзывы публичные, поэтому CORS открыт всем."""
        cors = {"Access-Control-Allow-Origin": "*"}
        with self._storage() as storage:
            try:
                site = self._site_or_404(storage, params.get("site", ""))
            except FileNotFoundError:
                self._json({"error": "сайт не найден"}, status=404, extra=cors)
                return

            settings = site.settings
            limit = _clamp(params.get("limit"), settings["limit"], 1, 100)
            min_rating = _clamp(params.get("min_rating"), settings["min_rating"], 1, 5)
            reviews = storage.published(site.key, limit=limit, min_rating=min_rating)
            stats = storage.stats(site.key, min_rating=min_rating)

        self._json(
            {
                "site": site.key,
                "name": site.name,
                "settings": settings,
                "stats": stats,
                "reviews": [review.public() for review in reviews],
            },
            cache=API_CACHE,
            extra=cors,
        )

    def _submit(self) -> None:
        """Приём отзыва из формы (со своей страницы или с чужого домена)."""
        payload = self._body()
        origin = self._origin()
        cors = {"Access-Control-Allow-Origin": origin or "*", "Vary": "Origin"}

        with self._storage() as storage:
            site = storage.get_site(str(payload.get("site", "")))
            if site is None:
                self._json({"error": "сайт не найден"}, status=404, extra=cors)
                return
            if origin and not self._same_host(origin) and not site.allows_origin(origin):
                self._json({"error": "домен не разрешён"}, status=403, extra=cors)
                return

            guard = check_submission(payload)
            if not guard.ok:
                self._json({"error": guard.reason}, status=400, extra=cors)
                return

            ip = self._client_ip()
            if not self.limiter.allow(f"{site.key}:{ip}"):
                self._json({"error": "Слишком много отзывов подряд, попробуйте позже"},
                           status=429, extra=cors)
                return

            try:
                review = Review.build(
                    site=site.key,
                    text=payload.get("text"),
                    rating=payload.get("rating"),
                    author=payload.get("author"),
                    contact=payload.get("contact"),
                    source="form",
                    meta={"ua": clean_text(self.headers.get("User-Agent"), 200)},
                )
            except ValidationError as error:
                self._json({"error": str(error)}, status=400, extra=cors)
                return

            verdict = check_text(review.text, review.author)
            if not verdict.ok:
                self._json({"error": verdict.reason}, status=400, extra=cors)
                return

            review.status = decide_status(site, review, verdict)
            if verdict.flags:
                review.meta["flags"] = verdict.flags
            saved, created = storage.add_review(review)

        if created:
            _notify_async(site, saved, self._base_url())
        self._json({"ok": True, "status": saved.status, "duplicate": not created}, extra=cors)

    # ------------------------------------------------------------ страницы

    def _form_page(self, key: str, params: dict[str, str]) -> None:
        with self._storage() as storage:
            site = self._site_or_404(storage, key)
        config = {
            "site": site.key,
            "name": site.name,
            "accent": site.settings.get("accent", "#2f6df6"),
            "embed": params.get("embed") == "1",
            "api": self.public_url.rstrip("/") if self.public_url else "",
        }
        html = self._page("form.html", {
            "__RW_FORM_CONFIG__": json.dumps(config, ensure_ascii=False),
        })
        self._send(html.encode("utf-8"))

    def _preview_page(self, key: str) -> None:
        with self._storage() as storage:
            site = self._site_or_404(storage, key)
        html = self._page("preview.html", {
            "__RW_SITE_KEY__": site.key,
            "__RW_THEME__": str(site.settings.get("theme", "auto")),
        })
        self._send(html.encode("utf-8"))

    def _landing(self) -> None:
        with self._storage() as storage:
            sites = storage.list_sites()
        key = sites[0].key if sites else ""
        html = self._page("index.html", {
            "__RW_SITE_KEY__": key,
            "__RW_BASE__": self._base_url(),
        })
        self._send(html.encode("utf-8"))

    def _static(self, name: str) -> None:
        body = self._asset(name)
        guessed = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if guessed.startswith("text/") or guessed.endswith("javascript"):
            guessed += "; charset=utf-8"
        self._send(body, guessed, cache=WIDGET_CACHE)

    # ------------------------------------------------------------- админка

    def _auth(self, storage: Storage) -> Site:
        key = self.headers.get("X-Site") or ""
        token = self.headers.get("X-Admin-Token") or ""
        site = storage.get_site(key) if _KEY_RE.match(key or "") else None
        if site is None or not site.check_token(token):
            raise PermissionError("нужен верный ключ сайта и токен")
        if site.token_needs_upgrade():
            # База из первых версий: токен лежал открытым текстом. Первый
            # удачный вход — подходящий момент заменить его хешем.
            storage.update_site(site.key, token_hash=hash_token(token))
        return site

    def _admin_site(self) -> None:
        with self._storage() as storage:
            site = self._auth(storage)
            payload = {
                "site": {
                    "key": site.key,
                    "name": site.name,
                    "domains": site.domains,
                    "auto_approve": site.auto_approve,
                    "auto_approve_from": site.auto_approve_from,
                    "telegram_token": site.telegram_token,
                    "telegram_chat": site.telegram_chat,
                    "settings": site.settings,
                },
                "counts": storage.counts(site.key),
            }
        self._json(payload)

    def _admin_update(self) -> None:
        data = self._body()
        fields: dict[str, Any] = {}
        if "settings" in data:
            fields["settings"] = data["settings"]
        if "name" in data:
            fields["name"] = clean_text(data["name"], 80)
        if "domains" in data:
            fields["domains"] = normalize_domains(data["domains"])
        if "auto_approve" in data:
            fields["auto_approve"] = data["auto_approve"] in (True, 1, "1", "true", "on")
        if "auto_approve_from" in data:
            try:
                fields["auto_approve_from"] = max(1, min(5, int(data["auto_approve_from"])))
            except (TypeError, ValueError):
                pass
        for key in ("telegram_token", "telegram_chat"):
            if key in data:
                fields[key] = clean_text(data[key], 120)

        with self._storage() as storage:
            site = self._auth(storage)
            updated = storage.update_site(site.key, **fields)
            payload = {
                "site": {
                    "key": updated.key,
                    "name": updated.name,
                    "domains": updated.domains,
                    "auto_approve": updated.auto_approve,
                    "auto_approve_from": updated.auto_approve_from,
                    "telegram_token": updated.telegram_token,
                    "telegram_chat": updated.telegram_chat,
                    "settings": updated.settings,
                },
                "counts": storage.counts(updated.key),
            }
        self._json(payload)

    def _admin_reviews(self, params: dict[str, str]) -> None:
        status = params.get("status")
        if status not in ("pending", "published", "rejected"):
            status = None
        limit = _clamp(params.get("limit"), 100, 1, 200)
        offset = _clamp(params.get("offset"), 0, 0, 100000)
        with self._storage() as storage:
            site = self._auth(storage)
            reviews = storage.list_reviews(site.key, status=status, limit=limit, offset=offset)
            payload = {
                "reviews": [review.admin() for review in reviews],
                "counts": storage.counts(site.key),
            }
        self._json(payload)

    # ------------------------------------------- импорт с площадок (дашборд)

    def _admin_platforms(self) -> None:
        """Что можно подтянуть из reviews-dashboard. Если он не подключён —
        честно говорим об этом и подсказываем команду."""
        with self._storage() as storage:
            self._auth(storage)

        if not self.dashboard_db:
            self._json({
                "available": False,
                "reason": "Импорт с площадок не подключён к этому серверу.",
                "hint": "Запустите сервер с флагом --dashboard-db "
                        "путь/к/reviews-dashboard/data/reviews.db",
                "companies": [],
            })
            return
        try:
            companies = dashboard.companies(self.dashboard_db)
        except DashboardError as error:
            self._json({"available": False, "reason": str(error), "companies": []})
            return
        self._json({"available": True, "companies": companies,
                    "platforms": dashboard.PLATFORMS})

    def _admin_pull(self) -> None:
        if not self.dashboard_db:
            self._json({"error": "импорт с площадок не подключён"}, status=400)
            return

        data = self._body()
        with self._storage() as storage:
            site = self._auth(storage)
            try:
                stats = dashboard.pull(
                    storage, site, self.dashboard_db,
                    company=clean_text(data.get("company"), 120) or None,
                    source=str(data.get("source") or "") or None,
                    min_rating=_clamp(data.get("min_rating"), 1, 1, 5),
                    since=clean_text(data.get("since"), 10) or None,
                    limit=_clamp(data.get("limit"), 200, 1, 1000),
                    publish=data.get("publish") in (True, 1, "1", "true", "on"),
                )
            except DashboardError as error:
                self._json({"error": str(error)}, status=400)
                return
            payload = {"ok": True, "stats": stats, "counts": storage.counts(site.key)}
        self._json(payload)

    def _admin_action(self, raw_id: str) -> None:
        try:
            review_id = int(raw_id)
        except ValueError:
            raise FileNotFoundError(raw_id) from None

        data = self._body()
        action = str(data.get("action", ""))
        with self._storage() as storage:
            site = self._auth(storage)
            review = storage.get_review(review_id)
            if review is None or review.site != site.key:
                self._json({"error": "отзыв не найден"}, status=404)
                return

            if action == "publish":
                review = storage.set_status(review_id, "published")
            elif action == "reject":
                review = storage.set_status(review_id, "rejected")
            elif action == "pin":
                review = storage.set_pinned(review_id, True)
            elif action == "unpin":
                review = storage.set_pinned(review_id, False)
            elif action == "reply":
                review = storage.set_reply(review_id, clean_text(data.get("reply"), MAX_REPLY))
            elif action == "delete":
                storage.delete_review(review_id)
                self._json({"ok": True, "deleted": review_id})
                return
            else:
                self._json({"error": "неизвестное действие"}, status=400)
                return
            payload = {"ok": True, "review": review.admin(), "counts": storage.counts(site.key)}
        self._json(payload)


def _clamp(raw: Any, fallback: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(raw)))
    except (TypeError, ValueError):
        return fallback


def _notify_async(site: Site, review: Review, base_url: str) -> None:
    """Уведомление в Telegram не должно задерживать ответ формы."""
    if not (site.telegram_token and site.telegram_chat):
        return
    thread = threading.Thread(
        target=telegram.notify_new_review, args=(site, review, base_url), daemon=True
    )
    thread.start()


def serve(host: str = "127.0.0.1", port: int = 8080, db_path: str = str(DEFAULT_DB),
          public_url: str = "", dashboard_db: str = "") -> None:
    WidgetHandler.db_path = db_path
    WidgetHandler.public_url = public_url
    WidgetHandler.dashboard_db = dashboard_db
    # Схему и WAL готовим один раз здесь, чтобы соединения запросов открывались
    # без DDL — на отдаче отзывов это была основная стоимость запроса.
    Storage(db_path).close()
    httpd = ThreadingHTTPServer((host, port), WidgetHandler)
    print(f"Виджет отзывов: http://{host}:{port}  (админка /admin, база {db_path})")
    if dashboard_db:
        print(f"Импорт с площадок: {dashboard_db}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено")
    finally:
        httpd.server_close()
