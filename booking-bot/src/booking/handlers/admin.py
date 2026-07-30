"""Админка: расписание дня, услуги, мастера, рабочие часы, выходные, статистика.

Всё делается командами, а не формами: владелец точки открывает бота с телефона
между клиентами, и «/dayoff 5 августа» быстрее пяти нажатий.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..config import Settings
from ..keyboards import CB_ADMIN_CANCEL, payload
from ..models import WEEKDAY_NAMES
from ..notify import Notifier
from ..schedule import format_minutes, parse_date, parse_schedule_spec
from ..service import BookingService
from ..storage import Storage
from ..texts import (
    day_schedule,
    escape,
    fmt_date,
    fmt_moment,
    fmt_price,
    human_duration,
)

log = logging.getLogger(__name__)

ADMIN_HELP = """<b>Админка</b>

<b>Расписание</b>
/today, /tomorrow — записи на день
/week — ближайшие 7 дней
/day 5.08 — конкретный день

<b>Услуги</b>
/services — список и переключатели
/service_add Стрижка | 60 | 1500 | описание
   название | минуты | цена | описание (последние два необязательны)

<b>Мастера</b>
/masters — список
/master_add Аня

<b>Рабочие часы</b>
/hours — текущее расписание
/hours пн-пт 10:00-20:00
/hours сб 11:00-16:00
/hours вс выходной
/hours пн-пт 10:00-14:00 15:00-20:00 — с перерывом

<b>Выходные и отпуска</b>
/dayoff 5.08 — закрыть день
/dayoff 5.08 12.08 отпуск — закрыть диапазон
/dayoffs — список и удаление

/stats — сводка за 30 дней"""


def build_admin_router(
    storage: Storage, booking: BookingService, notifier: Notifier, settings: Settings
) -> Router:
    router = Router(name="admin")
    admins = settings.admins()
    tz = settings.tz

    # Один фильтр на весь роутер: клиентские хендлеры лежат в соседнем роутере
    # и получают всё, что сюда не прошло.
    router.message.filter(F.from_user.id.in_(admins))
    router.callback_query.filter(F.from_user.id.in_(admins))

    # ── расписание ────────────────────────────────────────────────────────

    @router.message(Command("admin"))
    async def cmd_admin(message: Message) -> None:
        await message.answer(ADMIN_HELP)

    @router.message(Command("today"))
    async def cmd_today(message: Message) -> None:
        day = booking.today()
        items = await booking.day_bookings(day)
        await message.answer(day_schedule(items, tz, f"Сегодня, {fmt_date(day)}"))

    @router.message(Command("tomorrow"))
    async def cmd_tomorrow(message: Message) -> None:
        day = booking.today() + timedelta(days=1)
        items = await booking.day_bookings(day)
        await message.answer(day_schedule(items, tz, f"Завтра, {fmt_date(day)}"))

    @router.message(Command("day"))
    async def cmd_day(message: Message, command: CommandObject) -> None:
        try:
            day = parse_date(command.args or "", booking.today())
        except ValueError as exc:
            await message.answer(str(exc))
            return
        items = await booking.day_bookings(day)
        weekday = WEEKDAY_NAMES[day.weekday()]
        await message.answer(day_schedule(items, tz, f"{fmt_date(day)} ({weekday})"))

    @router.message(Command("week"))
    async def cmd_week(message: Message) -> None:
        today = booking.today()
        chunks: list[str] = []
        total = 0
        for shift in range(7):
            day = today + timedelta(days=shift)
            items = await booking.day_bookings(day)
            total += len(items)
            mark = "—" if not items else f"{len(items)}"
            chunks.append(f"{WEEKDAY_NAMES[day.weekday()]} {fmt_date(day)}: {mark}")
        await message.answer(
            "<b>Ближайшая неделя</b>\n\n" + "\n".join(chunks) + f"\n\nВсего записей: {total}"
        )

    # ── услуги ────────────────────────────────────────────────────────────

    @router.message(Command("services"))
    async def cmd_services(message: Message) -> None:
        services = await storage.list_services(only_active=False)
        if not services:
            await message.answer(
                "Услуг пока нет.\nДобавить: /service_add Стрижка | 60 | 1500"
            )
            return

        lines = ["<b>Услуги</b>", "", "Нажмите, чтобы скрыть или вернуть в список."]
        rows = [
            [
                InlineKeyboardButton(
                    text=f"{'✅' if item.active else '🚫'} {item.title} · "
                    f"{human_duration(item.duration_min)} · {fmt_price(item.price)}",
                    callback_data=f"svctoggle:{item.id}",
                )
            ]
            for item in services
        ]
        await message.answer(
            "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
        )

    @router.callback_query(F.data.startswith("svctoggle:"))
    async def toggle_service(callback: CallbackQuery) -> None:
        raw = payload(callback.data)
        service = await storage.get_service(int(raw)) if raw.isdigit() else None
        if service is None:
            await callback.answer("Услуга не найдена")
            return

        await storage.update_service(service.id, active=int(not service.active))
        await callback.answer("Скрыл" if service.active else "Вернул в список")

        services = await storage.list_services(only_active=False)
        rows = [
            [
                InlineKeyboardButton(
                    text=f"{'✅' if item.active else '🚫'} {item.title} · "
                    f"{human_duration(item.duration_min)} · {fmt_price(item.price)}",
                    callback_data=f"svctoggle:{item.id}",
                )
            ]
            for item in services
        ]
        if isinstance(callback.message, Message):
            await callback.message.edit_reply_markup(
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
            )

    @router.message(Command("service_add"))
    async def cmd_service_add(message: Message, command: CommandObject) -> None:
        parts = [part.strip() for part in (command.args or "").split("|")]
        if len(parts) < 2 or not parts[0]:
            await message.answer(
                "Формат: /service_add Стрижка | 60 | 1500 | мужская, с мытьём\n"
                "Обязательны только название и длительность в минутах."
            )
            return

        title = parts[0]
        duration = _digits(parts[1])
        if not duration:
            await message.answer("Длительность нужна в минутах, например 60.")
            return

        price = _digits(parts[2]) if len(parts) > 2 else None
        description = parts[3] if len(parts) > 3 else ""
        await storage.add_service(title, duration, price, description)
        await message.answer(
            f"Добавил: <b>{escape(title)}</b> · {human_duration(duration)}"
            + (f" · {fmt_price(price)}" if price else "")
        )

    # ── мастера ───────────────────────────────────────────────────────────

    @router.message(Command("masters"))
    async def cmd_masters(message: Message) -> None:
        masters = await storage.list_masters(only_active=False)
        if not masters:
            await message.answer(
                "Мастера не заведены — бот работает как единый календарь точки.\n"
                "Добавить: /master_add Аня"
            )
            return
        rows = [
            [
                InlineKeyboardButton(
                    text=f"{'✅' if item.active else '🚫'} {item.name}",
                    callback_data=f"mtoggle:{item.id}",
                )
            ]
            for item in masters
        ]
        await message.answer(
            "<b>Мастера</b>\n\nНажмите, чтобы включить или выключить.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @router.callback_query(F.data.startswith("mtoggle:"))
    async def toggle_master(callback: CallbackQuery) -> None:
        raw = payload(callback.data)
        master = await storage.get_master(int(raw)) if raw.isdigit() else None
        if master is None:
            await callback.answer("Мастер не найден")
            return
        await storage.set_master_active(master.id, not master.active)
        await callback.answer("Выключил" if master.active else "Включил")

    @router.message(Command("master_add"))
    async def cmd_master_add(message: Message, command: CommandObject) -> None:
        name = (command.args or "").strip()
        if not name:
            await message.answer("Формат: /master_add Аня")
            return
        await storage.add_master(name)
        await message.answer(f"Добавил мастера: <b>{escape(name)}</b>")

    # ── рабочие часы ──────────────────────────────────────────────────────

    @router.message(Command("hours"))
    async def cmd_hours(message: Message, command: CommandObject) -> None:
        raw = (command.args or "").strip()
        if not raw:
            await message.answer(await _hours_text(storage))
            return

        try:
            weekdays, windows = parse_schedule_spec(raw)
        except ValueError as exc:
            await message.answer(f"{exc}\n\nПример: /hours пн-пт 10:00-20:00")
            return

        await storage.set_windows(weekdays, windows)
        await message.answer("Записал.\n\n" + await _hours_text(storage))

    # ── выходные ──────────────────────────────────────────────────────────

    @router.message(Command("dayoff"))
    async def cmd_dayoff(message: Message, command: CommandObject) -> None:
        args = (command.args or "").split()
        if not args:
            await message.answer("Формат: /dayoff 5.08 или /dayoff 5.08 12.08 отпуск")
            return

        today = booking.today()
        try:
            first = parse_date(args[0], today)
            last = first
            note_from = 1
            if len(args) > 1:
                try:
                    last = parse_date(args[1], today)
                    note_from = 2
                except ValueError:
                    last = first
        except ValueError as exc:
            await message.answer(str(exc))
            return

        if last < first:
            first, last = last, first
        note = " ".join(args[note_from:])

        start = datetime(first.year, first.month, first.day, tzinfo=tz)
        end = datetime(last.year, last.month, last.day, tzinfo=tz) + timedelta(days=1)
        await storage.add_timeoff(start.astimezone(UTC), end.astimezone(UTC), note=note)

        affected = await storage.bookings_between(start.astimezone(UTC), end.astimezone(UTC))
        text = (
            f"Закрыл {fmt_date(first)}"
            if first == last
            else f"Закрыл с {fmt_date(first)} по {fmt_date(last)}"
        )
        if note:
            text += f" ({escape(note)})"
        if affected:
            # Записи, попавшие в отпуск, не отменяем молча: решает владелец.
            text += (
                f"\n\n⚠️ На эти дни уже есть записей: {len(affected)}. "
                "Посмотреть — /day, отменить — кнопкой под записью."
            )
        await message.answer(text)

    @router.message(Command("dayoffs"))
    async def cmd_dayoffs(message: Message) -> None:
        items = await storage.list_timeoff(since=datetime.now(UTC))
        if not items:
            await message.answer("Закрытых дней впереди нет.")
            return

        rows = []
        for timeoff_id, interval, _master_id, note in items:
            start = interval.start.astimezone(tz).date()
            end = (interval.end.astimezone(tz) - timedelta(minutes=1)).date()
            label = fmt_date(start) if start == end else f"{fmt_date(start)} – {fmt_date(end)}"
            if note:
                label += f" · {note}"
            rows.append(
                [InlineKeyboardButton(text=f"🗑 {label}", callback_data=f"offdel:{timeoff_id}")]
            )
        await message.answer(
            "<b>Закрытые дни</b>\n\nНажмите, чтобы снять.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @router.callback_query(F.data.startswith("offdel:"))
    async def delete_dayoff(callback: CallbackQuery) -> None:
        raw = payload(callback.data)
        if raw.isdigit():
            await storage.delete_timeoff(int(raw))
        await callback.answer("Снял")
        if isinstance(callback.message, Message):
            await callback.message.edit_text("Готово. Актуальный список — /dayoffs")

    # ── статистика ────────────────────────────────────────────────────────

    @router.message(Command("stats"))
    async def cmd_stats(message: Message) -> None:
        stats = await storage.stats(30)
        top = await storage.top_services(30)
        clients = await storage.count_clients()

        lines = [
            "<b>За 30 дней</b>",
            "",
            f"Записей создано: {stats['total']}",
            f"Состоялось: {stats['done']}",
            f"Впереди: {stats['active']}",
            f"Отменено: {stats['cancelled']}",
            f"Клиентов в базе: {clients}",
        ]
        if top:
            lines.extend(["", "<b>Популярные услуги</b>"])
            lines.extend(f"• {escape(title)} — {count}" for title, count in top)
        await message.answer("\n".join(lines))

    # ── отмена записи админом ─────────────────────────────────────────────

    @router.callback_query(F.data.startswith(f"{CB_ADMIN_CANCEL}:"))
    async def admin_cancel(callback: CallbackQuery) -> None:
        raw = payload(callback.data)
        cancelled = await storage.cancel_booking(int(raw), by="admin") if raw.isdigit() else None
        if cancelled is None:
            await callback.answer("Запись уже неактивна")
            return

        await callback.answer("Отменил")
        if isinstance(callback.message, Message):
            await callback.message.edit_reply_markup(reply_markup=None)

        await notifier.notify_client(
            cancelled.client_id,
            f"❌ К сожалению, запись «{escape(cancelled.service_title)}» "
            f"на {fmt_moment(cancelled.starts_at, tz)} отменена.\n"
            "Свяжитесь с нами, чтобы подобрать другое время.",
        )
        await notifier.cancelled(cancelled, by_client=False)

    return router


# ──────────────────────────────────────────────────────────────────────────────
# Вспомогательное
# ──────────────────────────────────────────────────────────────────────────────


async def _hours_text(storage: Storage) -> str:
    windows = await storage.list_windows()
    if not windows:
        return "Расписание не задано, записаться нельзя.\nПример: /hours пн-пт 10:00-20:00"

    by_day: dict[int, list[str]] = {}
    for window in windows:
        if window.master_id is not None:
            continue
        by_day.setdefault(window.weekday, []).append(
            f"{format_minutes(window.start_min)}–{format_minutes(window.end_min)}"
        )

    lines = ["<b>Рабочие часы</b>", ""]
    for weekday in range(7):
        slots = by_day.get(weekday)
        lines.append(f"{WEEKDAY_NAMES[weekday]}: {', '.join(slots) if slots else 'выходной'}")
    return "\n".join(lines)


def _digits(raw: str) -> int | None:
    value = "".join(char for char in raw if char.isdigit())
    return int(value) if value else None


def admin_commands() -> list[BotCommand]:
    return [
        BotCommand(command="today", description="Записи на сегодня"),
        BotCommand(command="tomorrow", description="Записи на завтра"),
        BotCommand(command="week", description="Ближайшая неделя"),
        BotCommand(command="services", description="Услуги"),
        BotCommand(command="hours", description="Рабочие часы"),
        BotCommand(command="dayoff", description="Закрыть день"),
        BotCommand(command="stats", description="Статистика"),
        BotCommand(command="admin", description="Справка админа"),
    ]
