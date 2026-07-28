"""Telegram: сбор отзывов ботом и уведомления владельцу сайта.

Bot API дёргается через urllib — никакой aiogram здесь не нужен, весь бот
это один цикл getUpdates. Диалог короткий: оценка → текст → подпись.
Владельцу отзыв приходит с кнопками «Опубликовать» / «Отклонить», так что
модерация возможна вообще без захода в админку.
"""

from __future__ import annotations

import html
import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from .models import Review, Site, ValidationError
from .moderation import check_text, decide_status
from .storage import Storage

API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 40
POLL_TIMEOUT = 25

STARS = ["", "★☆☆☆☆", "★★☆☆☆", "★★★☆☆", "★★★★☆", "★★★★★"]

GREETING = (
    "Здравствуйте! Здесь можно оставить отзыв о компании «{name}».\n\n"
    "Насколько всё прошло хорошо?"
)
ASK_TEXT = "Спасибо! Теперь пара слов — что понравилось, а что нет."
ASK_SIGN = "Как подписать отзыв?"
THANKS_PENDING = "Готово! Отзыв ушёл на проверку — скоро появится на сайте."
THANKS_LIVE = "Готово! Отзыв уже опубликован на сайте. Спасибо, что нашли время."


class TelegramError(RuntimeError):
    pass


def call(token: str, method: str, payload: dict[str, Any] | None = None,
         timeout: int = TIMEOUT) -> dict[str, Any]:
    """Вызов метода Bot API. Возвращает поле `result`."""
    data = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        API.format(token=token, method=method),
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:200]
        raise TelegramError(f"{method}: HTTP {error.code} {detail}") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise TelegramError(f"{method}: {error}") from None
    if not body.get("ok"):
        raise TelegramError(f"{method}: {body.get('description')}")
    return body.get("result")


def send(token: str, chat_id: Any, text: str, keyboard: list[list[dict]] | None = None,
         **extra: Any) -> dict[str, Any] | None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    payload.update(extra)
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    try:
        return call(token, "sendMessage", payload)
    except TelegramError as error:
        print(f"[telegram] не отправилось: {error}")
        return None


def notify_new_review(site: Site, review: Review, base_url: str = "") -> None:
    """Сообщение владельцу о новом отзыве с кнопками модерации."""
    status = "опубликован" if review.status == "published" else "ждёт проверки"
    text = (
        f"<b>Новый отзыв</b> — {html.escape(site.name)}\n"
        f"{STARS[review.rating]}  ({status})\n\n"
        f"{html.escape(review.text[:600])}\n\n"
        f"<i>{html.escape(review.author)}</i>"
    )
    if review.contact:
        text += f"\nКонтакт: {html.escape(review.contact)}"

    keyboard = []
    if review.status != "published":
        keyboard.append([
            {"text": "✅ Опубликовать", "callback_data": f"pub:{review.id}"},
            {"text": "🚫 Отклонить", "callback_data": f"rej:{review.id}"},
        ])
    if base_url:
        keyboard.append([{"text": "Открыть админку", "url": f"{base_url}/admin?site={site.key}"}])
    send(site.telegram_token, site.telegram_chat, text, keyboard or None)


class ReviewBot:
    """Бот одного сайта: собирает отзывы и обслуживает кнопки модерации."""

    def __init__(self, site_key: str, db_path: str) -> None:
        self.site_key = site_key
        self.db_path = db_path
        self.offset = 0
        self.states: dict[int, dict[str, Any]] = {}
        self.stop = threading.Event()

    # ------------------------------------------------------------ хелперы

    def site(self, storage: Storage) -> Site:
        site = storage.get_site(self.site_key)
        if site is None:
            raise TelegramError(f"сайт {self.site_key} не найден")
        return site

    def rating_keyboard(self) -> list[list[dict]]:
        return [[{"text": STARS[value], "callback_data": f"rate:{value}"}]
                for value in range(5, 0, -1)]

    # -------------------------------------------------------------- цикл

    def run(self) -> None:
        with Storage(self.db_path) as storage:
            site = self.site(storage)
            token = site.telegram_token
            if not token:
                raise TelegramError(f"у сайта {site.key} не задан токен бота")
            me = call(token, "getMe")
            print(f"[telegram] @{me.get('username')} собирает отзывы для «{site.name}»")

            while not self.stop.is_set():
                try:
                    updates = call(
                        token,
                        "getUpdates",
                        {"offset": self.offset, "timeout": POLL_TIMEOUT,
                         "allowed_updates": ["message", "callback_query"]},
                        timeout=POLL_TIMEOUT + 10,
                    )
                except TelegramError as error:
                    print(f"[telegram] {error}; пауза 5 секунд")
                    time.sleep(5)
                    continue

                for update in updates or []:
                    self.offset = int(update["update_id"]) + 1
                    try:
                        self.handle(storage, update)
                    except Exception as error:  # noqa: BLE001 — один сбойный апдейт не роняет бота
                        print(f"[telegram] апдейт пропущен: {error}")

    def handle(self, storage: Storage, update: dict[str, Any]) -> None:
        site = self.site(storage)
        if "callback_query" in update:
            self.on_callback(storage, site, update["callback_query"])
        elif "message" in update:
            self.on_message(storage, site, update["message"])

    # ----------------------------------------------------------- сценарий

    def on_message(self, storage: Storage, site: Site, message: dict[str, Any]) -> None:
        chat_id = message["chat"]["id"]
        text = (message.get("text") or "").strip()
        token = site.telegram_token

        if text.startswith("/start") or text.startswith("/otzyv"):
            self.states[chat_id] = {"step": "rating"}
            send(token, chat_id, GREETING.format(name=html.escape(site.name)),
                 self.rating_keyboard())
            return

        if text.startswith("/cancel"):
            self.states.pop(chat_id, None)
            send(token, chat_id, "Отменил. Если передумаете — /start.")
            return

        state = self.states.get(chat_id)
        if not state or state.get("step") != "text":
            send(token, chat_id, "Чтобы оставить отзыв, нажмите /start.")
            return

        if len(text) < 8:
            send(token, chat_id, "Слишком коротко — напишите хотя бы пару предложений.")
            return

        state["text"] = text
        state["step"] = "sign"
        name = html.escape((message.get("from") or {}).get("first_name") or "Аноним")
        send(token, chat_id, ASK_SIGN, [[
            {"text": f"Подписать: {name}", "callback_data": "sign:name"},
            {"text": "Анонимно", "callback_data": "sign:anon"},
        ]])

    def on_callback(self, storage: Storage, site: Site, query: dict[str, Any]) -> None:
        token = site.telegram_token
        data = query.get("data") or ""
        chat_id = ((query.get("message") or {}).get("chat") or {}).get("id")
        answer = ""

        if data.startswith("rate:"):
            self.states[chat_id] = {"step": "text", "rating": int(data.split(":")[1])}
            send(token, chat_id, ASK_TEXT)
            answer = "Оценка принята"

        elif data.startswith("sign:"):
            state = self.states.get(chat_id) or {}
            if state.get("step") != "sign":
                answer = "Отзыв уже отправлен"
            else:
                author = "Аноним"
                if data.endswith(":name"):
                    author = (query.get("from") or {}).get("first_name") or "Аноним"
                answer = self.save(storage, site, chat_id, state, author)
                self.states.pop(chat_id, None)

        elif data.startswith(("pub:", "rej:")):
            answer = self.moderate(storage, site, query, data)

        try:
            call(token, "answerCallbackQuery", {"callback_query_id": query["id"], "text": answer})
        except TelegramError:
            pass

    def save(self, storage: Storage, site: Site, chat_id: int, state: dict[str, Any],
             author: str) -> str:
        try:
            review = Review.build(
                site=site.key,
                text=state.get("text", ""),
                rating=state.get("rating", 5),
                author=author,
                source="telegram",
                meta={"chat_id": chat_id},
            )
        except ValidationError as error:
            send(site.telegram_token, chat_id, str(error))
            return "Не получилось"

        verdict = check_text(review.text, review.author)
        if not verdict.ok:
            send(site.telegram_token, chat_id, "Отзыв похож на рекламу и не был принят.")
            return "Отклонено"

        review.status = decide_status(site, review, verdict)
        if verdict.flags:
            review.meta["flags"] = verdict.flags
        saved, created = storage.add_review(review)

        if not created:
            send(site.telegram_token, chat_id, "Такой отзыв уже есть — спасибо!")
            return "Уже есть"

        send(site.telegram_token, chat_id,
             THANKS_LIVE if saved.status == "published" else THANKS_PENDING)
        if site.telegram_chat and str(site.telegram_chat) != str(chat_id):
            notify_new_review(site, saved)
        return "Спасибо!"

    def moderate(self, storage: Storage, site: Site, query: dict[str, Any], data: str) -> str:
        """Кнопки под уведомлением работают только в чате владельца."""
        chat_id = ((query.get("message") or {}).get("chat") or {}).get("id")
        if not site.telegram_chat or str(chat_id) != str(site.telegram_chat):
            return "Нет прав"

        action, raw_id = data.split(":", 1)
        review = storage.get_review(int(raw_id))
        if review is None or review.site != site.key:
            return "Отзыв не найден"

        status = "published" if action == "pub" else "rejected"
        storage.set_status(review.id, status)
        message = query.get("message") or {}
        try:
            call(site.telegram_token, "editMessageReplyMarkup", {
                "chat_id": chat_id,
                "message_id": message.get("message_id"),
                "reply_markup": {"inline_keyboard": []},
            })
        except TelegramError:
            pass
        return "Опубликован" if status == "published" else "Отклонён"


def run_bots(db_path: str, site_keys: list[str]) -> None:
    """Поднимает по потоку на сайт — у каждого клиента свой бот и свой токен."""
    bots = [ReviewBot(key, db_path) for key in site_keys]
    threads = [threading.Thread(target=_guard, args=(bot,), daemon=True) for bot in bots]
    for thread in threads:
        thread.start()
    try:
        while any(thread.is_alive() for thread in threads):
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nОстанавливаю ботов…")
        for bot in bots:
            bot.stop.set()


def _guard(bot: ReviewBot) -> None:
    try:
        bot.run()
    except TelegramError as error:
        print(f"[telegram] {bot.site_key}: {error}")
