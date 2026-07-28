"""Telegram-бот: настройка фильтров на ходу.

Смысл в том, чтобы менять ключевые слова и порог бюджета с телефона, не заходя
на сервер и не перезапуская контейнер. Настройки лежат в БД, поллер читает их
на каждом опросе — правка применяется со следующего цикла.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .formatter import escape, format_budget, humanize_age, source_title
from .models import Order
from .sender import CALLBACK_FAVORITE, CALLBACK_STOPWORD
from .sources import ALL_SLUGS, source_titles
from .storage import Filters, Storage

if TYPE_CHECKING:
    from .poller import Poller

log = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand(command="status", description="Что сейчас происходит"),
    BotCommand(command="kw", description="Ключевые слова"),
    BotCommand(command="kw_add", description="Добавить ключевые слова"),
    BotCommand(command="kw_del", description="Убрать ключевое слово"),
    BotCommand(command="stopwords", description="Стоп-слова"),
    BotCommand(command="stop_add", description="Добавить стоп-слова"),
    BotCommand(command="stop_del", description="Убрать стоп-слово"),
    BotCommand(command="budget", description="Минимальный бюджет"),
    BotCommand(command="sources", description="Площадки"),
    BotCommand(command="categories", description="Рубрики"),
    BotCommand(command="last", description="Последние отправленные"),
    BotCommand(command="favorites", description="Избранное"),
    BotCommand(command="stats", description="Статистика за неделю"),
    BotCommand(command="check", description="Опросить биржи прямо сейчас"),
    BotCommand(command="pause", description="Пауза"),
    BotCommand(command="resume", description="Продолжить"),
    BotCommand(command="help", description="Справка"),
]

HELP_TEXT = """<b>Парсер заказов с фриланс-бирж</b>

Слежу за Kwork, FL.ru, Habr Freelance и Weblancer и присылаю подходящие заказы.

<b>Фильтры</b>
/kw — ключевые слова, /kw_add питон бот, /kw_del бот
/stopwords — стоп-слова, /stop_add логотип, /stop_del логотип
/budget 5000 — минимальный бюджет (0 — без порога)
/sources — включить и выключить площадки
/categories — отбор по рубрикам

<b>Что происходит</b>
/status — состояние опроса
/last 5 — последние отправленные заказы
/favorites — сохранённые
/stats — сводка за неделю
/check — опросить биржи немедленно
/pause, /resume — приостановить и вернуть рассылку

Если ключевых слов нет, проходят все заказы, кроме отсеянных стоп-словами,
бюджетом и рубриками."""

_WORD_SPLIT_RE = re.compile(r"[,\s]+")
#: Слова короче этого в кандидаты в стоп-слова не берём — от «для» и «под»
#: фильтру пользы нет, а промахнуться ими легко.
_MIN_STOPWORD_LENGTH = 4


def build_router(storage: Storage, *, owner_id: int, poller: Poller | None = None) -> Router:
    router = Router(name="owner")

    # Единственная проверка доступа: бота могут найти по имени, а настройки
    # отбора и история заказов — не то, что стоит показывать посторонним.
    router.message.filter(F.from_user.id == owner_id)
    router.callback_query.filter(F.from_user.id == owner_id)

    _register_handlers(router, storage, poller)
    return router


def _register_handlers(router: Router, storage: Storage, poller: Poller | None) -> None:
    # ── справка и состояние ───────────────────────────────────────────────

    @router.message(Command("start", "help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(HELP_TEXT, parse_mode="HTML")

    @router.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        filters = await storage.get_filters()
        total = await storage.count_orders()
        last_poll = await storage.get_state("last_poll_at")

        lines = [
            "⏸ <b>На паузе</b>" if filters.paused else "▶️ <b>Работаю</b>",
            "",
            f"Площадки: {_format_sources(filters)}",
            f"Ключевые слова: {_format_list(filters.keywords) or 'не заданы (пропускаю всё)'}",
            f"Стоп-слова: {_format_list(filters.stopwords) or 'нет'}",
            f"Минимальный бюджет: {filters.min_budget or 'без порога'}",
            f"Рубрики: {_format_list(filters.categories) or 'все'}",
            "",
            f"В истории заказов: {total}",
            f"Последний опрос: {_format_moment(last_poll)}",
        ]
        if poller is not None and poller.last_error:
            lines.append(f"⚠️ Последняя ошибка: {escape(poller.last_error)}")

        await message.answer("\n".join(lines), parse_mode="HTML")

    # ── ключевые и стоп-слова ─────────────────────────────────────────────

    @router.message(Command("kw"))
    async def cmd_keywords(message: Message) -> None:
        filters = await storage.get_filters()
        await message.answer(
            f"🔑 Ключевые слова: {_format_list(filters.keywords) or 'не заданы'}\n\n"
            "Добавить: /kw_add питон бот\nУбрать: /kw_del бот",
            parse_mode="HTML",
        )

    @router.message(Command("kw_add"))
    async def cmd_keyword_add(message: Message, command: CommandObject) -> None:
        await _add_words(message, command, storage, "keywords", "ключевые слова")

    @router.message(Command("kw_del"))
    async def cmd_keyword_del(message: Message, command: CommandObject) -> None:
        await _remove_words(message, command, storage, "keywords", "ключевых слов")

    @router.message(Command("stopwords"))
    async def cmd_stopwords(message: Message) -> None:
        filters = await storage.get_filters()
        await message.answer(
            f"🔕 Стоп-слова: {_format_list(filters.stopwords) or 'нет'}\n\n"
            "Добавить: /stop_add логотип\nУбрать: /stop_del логотип",
            parse_mode="HTML",
        )

    @router.message(Command("stop_add"))
    async def cmd_stopword_add(message: Message, command: CommandObject) -> None:
        await _add_words(message, command, storage, "stopwords", "стоп-слова")

    @router.message(Command("stop_del"))
    async def cmd_stopword_del(message: Message, command: CommandObject) -> None:
        await _remove_words(message, command, storage, "stopwords", "стоп-слов")

    # ── бюджет ────────────────────────────────────────────────────────────

    @router.message(Command("budget"))
    async def cmd_budget(message: Message, command: CommandObject) -> None:
        filters = await storage.get_filters()
        raw = (command.args or "").strip()

        if not raw:
            await message.answer(
                f"💰 Минимальный бюджет: {filters.min_budget or 'без порога'}\n\n"
                "Изменить: /budget 5000\nСнять порог: /budget 0"
            )
            return

        digits = re.sub(r"[^\d]", "", raw)
        if not digits:
            await message.answer("Не понял сумму. Пример: /budget 5000")
            return

        filters.min_budget = int(digits)
        await storage.save_filters(filters)
        await message.answer(
            f"💰 Порог: {filters.min_budget}"
            if filters.min_budget
            else "💰 Порог снят, бюджет больше не фильтрую."
        )

    # ── площадки ──────────────────────────────────────────────────────────

    @router.message(Command("sources"))
    async def cmd_sources(message: Message) -> None:
        filters = await storage.get_filters()
        await message.answer(
            "🌐 Площадки — нажми, чтобы включить или выключить:",
            reply_markup=_sources_keyboard(filters),
        )

    @router.callback_query(F.data.startswith("src:"))
    async def toggle_source(callback: CallbackQuery) -> None:
        slug = (callback.data or "").split(":", 1)[1]
        if slug not in ALL_SLUGS:
            await callback.answer("Неизвестная площадка")
            return

        filters = await storage.get_filters()
        if slug in filters.sources:
            filters.sources = [item for item in filters.sources if item != slug]
            note = f"{source_title(slug)} выключена"
        else:
            # Порядок держим как в реестре, чтобы список не прыгал при каждом
            # включении и выключении.
            filters.sources = [item for item in ALL_SLUGS if item in {*filters.sources, slug}]
            note = f"{source_title(slug)} включена"

        await storage.save_filters(filters)
        await callback.answer(note)
        if isinstance(callback.message, Message):
            await callback.message.edit_reply_markup(reply_markup=_sources_keyboard(filters))

    # ── рубрики ───────────────────────────────────────────────────────────

    @router.message(Command("categories"))
    async def cmd_categories(message: Message) -> None:
        filters = await storage.get_filters()
        known = await storage.known_categories()
        if not known:
            await message.answer(
                "Рубрики появятся здесь после первого опроса — я собираю их из "
                "того, что реально приходит с бирж, а не из зашитого списка."
            )
            return

        await message.answer(
            "🏷 Рубрики. Отмеченные — те, что пропускаю.\n"
            "Если не отмечена ни одна, пропускаю все.",
            reply_markup=_categories_keyboard(known, filters),
        )

    @router.callback_query(F.data.startswith("cat:"))
    async def toggle_category(callback: CallbackQuery) -> None:
        raw_id = (callback.data or "").split(":", 1)[1]
        row = await storage.category_by_id(int(raw_id)) if raw_id.isdigit() else None
        if row is None:
            await callback.answer("Рубрика не найдена")
            return

        name = row["category"]
        filters = await storage.get_filters()
        if name in filters.categories:
            filters.categories = [item for item in filters.categories if item != name]
            note = f"«{name}» убрана"
        else:
            filters.categories = [*filters.categories, name]
            note = f"«{name}» добавлена"

        await storage.save_filters(filters)
        await callback.answer(note)
        if isinstance(callback.message, Message):
            known = await storage.known_categories()
            await callback.message.edit_reply_markup(
                reply_markup=_categories_keyboard(known, filters)
            )

    # ── пауза ─────────────────────────────────────────────────────────────

    @router.message(Command("pause"))
    async def cmd_pause(message: Message) -> None:
        filters = await storage.get_filters()
        filters.paused = True
        await storage.save_filters(filters)
        await message.answer("⏸ Пауза. Опрос продолжается, но в канал ничего не уходит.")

    @router.message(Command("resume"))
    async def cmd_resume(message: Message) -> None:
        filters = await storage.get_filters()
        filters.paused = False
        await storage.save_filters(filters)
        await message.answer("▶️ Продолжаю рассылку.")

    # ── история ───────────────────────────────────────────────────────────

    @router.message(Command("last"))
    async def cmd_last(message: Message, command: CommandObject) -> None:
        limit = _parse_limit(command.args, default=5, maximum=20)
        rows = await storage.last_sent(limit)
        if not rows:
            await message.answer("Пока ничего не отправлял.")
            return

        lines = [f"📜 <b>Последние {len(rows)}</b>", ""]
        for row in rows:
            budget = _format_row_budget(row)
            lines.append(
                f"• <a href=\"{escape(row['url'])}\">{escape(row['title'])}</a>\n"
                f"  {escape(budget)} · {escape(source_title(row['source']))}"
            )
        await message.answer("\n".join(lines), parse_mode="HTML")

    @router.message(Command("favorites"))
    async def cmd_favorites(message: Message) -> None:
        rows = await storage.list_favorites()
        if not rows:
            await message.answer("Избранное пусто. Кнопка ⭐ под заказом добавляет его сюда.")
            return

        lines = ["⭐ <b>Избранное</b>", ""]
        lines.extend(
            f"• <a href=\"{escape(row['url'])}\">{escape(row['title'])}</a>" for row in rows
        )
        await message.answer("\n".join(lines), parse_mode="HTML")

    @router.message(Command("stats"))
    async def cmd_stats(message: Message) -> None:
        rows = await storage.stats_since(7)
        if not rows:
            await message.answer("Статистики пока нет — нужен хотя бы один опрос.")
            return

        lines = ["📊 <b>За последние 7 дней</b>", ""]
        total_found = total_sent = 0
        for row in rows:
            found, sent = int(row["found"] or 0), int(row["sent"] or 0)
            total_found += found
            total_sent += sent
            lines.append(f"• {escape(source_title(row['source']))}: {found} → {sent} подошло")
        lines.extend(["", f"Всего: {total_found} просмотрено, {total_sent} отправлено"])
        await message.answer("\n".join(lines), parse_mode="HTML")

    @router.message(Command("check"))
    async def cmd_check(message: Message) -> None:
        if poller is None:
            await message.answer("Опрос недоступен в этом режиме.")
            return

        await message.answer("🔄 Опрашиваю биржи…")
        report = await poller.process()
        if not report.sources:
            await message.answer("Опрос на паузе — сними её командой /resume.")
            return

        lines = ["Готово.", ""]
        for source_report in report.sources:
            if source_report.skipped:
                lines.append(f"• {source_title(source_report.slug)}: {source_report.skipped}")
            elif source_report.error:
                lines.append(f"• {source_title(source_report.slug)}: ⚠️ {source_report.error}")
            else:
                lines.append(
                    f"• {source_title(source_report.slug)}: найдено {source_report.found}, "
                    f"новых {source_report.fresh}, подошло {source_report.accepted}"
                )
        await message.answer("\n".join(lines))

    # ── кнопки под заказом ────────────────────────────────────────────────

    @router.callback_query(F.data.startswith(f"{CALLBACK_FAVORITE}:"))
    async def add_favorite(callback: CallbackQuery) -> None:
        uid = (callback.data or "").split(":", 1)[1]
        row = await storage.order_by_uid(uid)
        if row is None:
            await callback.answer("Заказ не найден в истории")
            return

        await storage.add_favorite(uid, row["title"], row["url"])
        await callback.answer("⭐ Сохранено в избранное")

    @router.callback_query(F.data.startswith(f"{CALLBACK_STOPWORD}:"))
    async def suggest_stopword(callback: CallbackQuery) -> None:
        uid = (callback.data or "").split(":", 1)[1]
        row = await storage.order_by_uid(uid)
        if row is None:
            await callback.answer("Заказ не найден в истории")
            return

        filters = await storage.get_filters()
        words = _stopword_candidates(row["title"], filters.stopwords)
        if not words:
            await callback.answer("Не нашёл подходящего слова в заголовке")
            return

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=word, callback_data=f"stopw:{word}")]
                for word in words[:6]
            ]
        )
        # Заказ приходит в канал, а настройки — личное дело владельца, поэтому
        # выбор слова уводим в личку.
        await callback.bot.send_message(
            chat_id=callback.from_user.id,
            text=f"Какое слово из заголовка добавить в стоп-лист?\n\n<i>{escape(row['title'])}</i>",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        await callback.answer("Прислал варианты в личку")

    @router.callback_query(F.data.startswith("stopw:"))
    async def confirm_stopword(callback: CallbackQuery) -> None:
        word = (callback.data or "").split(":", 1)[1]
        filters = await storage.get_filters()
        if word not in filters.stopwords:
            filters.stopwords = [*filters.stopwords, word]
            await storage.save_filters(filters)

        await callback.answer(f"Добавил «{word}» в стоп-слова")
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                f"🔕 «{escape(word)}» теперь в стоп-словах.\n"
                f"Весь список: {_format_list(filters.stopwords)}",
                parse_mode="HTML",
            )


# ──────────────────────────────────────────────────────────────────────────────
# Вспомогательное
# ──────────────────────────────────────────────────────────────────────────────


async def _add_words(
    message: Message,
    command: CommandObject,
    storage: Storage,
    field: str,
    label: str,
) -> None:
    words = _split_words(command.args)
    if not words:
        await message.answer(f"Что добавить? Пример: /{command.command} питон бот")
        return

    filters = await storage.get_filters()
    current: list[str] = list(getattr(filters, field))
    added = [word for word in words if word not in current]
    if not added:
        await message.answer("Всё это уже в списке.")
        return

    setattr(filters, field, [*current, *added])
    await storage.save_filters(filters)
    await message.answer(
        f"Добавил в {label}: {_format_list(added)}\n\n"
        f"Теперь: {_format_list(getattr(filters, field))}",
        parse_mode="HTML",
    )


async def _remove_words(
    message: Message,
    command: CommandObject,
    storage: Storage,
    field: str,
    label: str,
) -> None:
    words = _split_words(command.args)
    if not words:
        await message.answer(f"Что убрать? Пример: /{command.command} бот")
        return

    filters = await storage.get_filters()
    current: list[str] = list(getattr(filters, field))
    remaining = [word for word in current if word not in words]
    if len(remaining) == len(current):
        await message.answer("Ничего из этого в списке нет.")
        return

    setattr(filters, field, remaining)
    await storage.save_filters(filters)
    await message.answer(
        f"Убрал из {label}. Осталось: {_format_list(remaining) or 'пусто'}",
        parse_mode="HTML",
    )


def _split_words(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [word.strip().lower() for word in _WORD_SPLIT_RE.split(raw) if word.strip()]


def _format_list(items: list[str]) -> str:
    return ", ".join(f"<code>{escape(item)}</code>" for item in items)


def _format_sources(filters: Filters) -> str:
    titles = source_titles()
    return ", ".join(
        f"{'✅' if slug in filters.sources else '❌'} {titles[slug]}" for slug in ALL_SLUGS
    )


def _format_moment(raw: str | None) -> str:
    if not raw:
        return "ещё не было"
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    return humanize_age(moment) or "только что"


def _format_row_budget(row) -> str:
    return format_budget(
        Order(
            source=row["source"],
            native_id="",
            title="",
            url="",
            budget_min=row["budget_min"],
            budget_max=row["budget_max"],
        )
    )


def _parse_limit(raw: str | None, *, default: int, maximum: int) -> int:
    digits = re.sub(r"[^\d]", "", raw or "")
    if not digits:
        return default
    return max(1, min(int(digits), maximum))


def _sources_keyboard(filters: Filters) -> InlineKeyboardMarkup:
    titles = source_titles()
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{'✅' if slug in filters.sources else '❌'} {titles[slug]}",
                    callback_data=f"src:{slug}",
                )
            ]
            for slug in ALL_SLUGS
        ]
    )


def _categories_keyboard(rows, filters: Filters) -> InlineKeyboardMarkup:
    selected = set(filters.categories)
    buttons = [
        [
            InlineKeyboardButton(
                text=f"{'✅' if row['category'] in selected else '▫️'} "
                f"{row['category']} ({source_title(row['source'])})",
                callback_data=f"cat:{row['id']}",
            )
        ]
        for row in rows[:40]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _stopword_candidates(title: str, existing: list[str]) -> list[str]:
    """Слова из заголовка, которые имеет смысл предложить как стоп-слово."""
    known = {word.lower() for word in existing}
    candidates: list[str] = []
    for raw in re.findall(r"[\w-]+", title.lower()):
        word = raw.strip("-")
        if len(word) < _MIN_STOPWORD_LENGTH or word.isdigit() or word in known:
            continue
        if word not in candidates:
            candidates.append(word)
    return candidates


async def setup_commands(bot: Bot) -> None:
    """Показать список команд в меню Telegram."""
    try:
        await bot.set_my_commands(BOT_COMMANDS)
    except Exception:
        # Меню — украшение; падать из-за него при старте не стоит.
        log.warning("не удалось установить меню команд", exc_info=True)
