"""Клиентская часть: запись, свои записи, отмена и перенос.

Сценарий один и тот же для новой записи и для переноса — меняется только то,
что происходит на последнем шаге. Поэтому режим лежит в состоянии (`mode`), а
шаги выбора услуги, дня и времени переиспользуются целиком.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Contact, Message, ReplyKeyboardRemove

from ..config import Settings
from ..keyboards import (
    BTN_BOOK,
    BTN_CONTACTS,
    BTN_MY,
    BTN_SERVICES,
    CB_BACK,
    CB_CANCEL,
    CB_CANCEL_YES,
    CB_CONFIRM,
    CB_DAY,
    CB_MASTER,
    CB_MOVE,
    CB_NOOP,
    CB_SERVICE,
    CB_SLOT,
    booking_keyboard,
    cancel_confirm_keyboard,
    confirm_keyboard,
    days_keyboard,
    main_menu,
    masters_keyboard,
    payload,
    request_phone,
    services_keyboard,
    slot_from_payload,
    slots_keyboard,
)
from ..models import Service
from ..notify import Notifier
from ..service import BookingService
from ..storage import SlotTaken, Storage
from ..texts import (
    booking_card,
    escape,
    fmt_date,
    fmt_moment,
    fmt_price,
    human_duration,
    services_list,
)

log = logging.getLogger(__name__)

MODE_NEW = "new"
MODE_MOVE = "move"

_PHONE_RE = re.compile(r"[\d+()\-\s]{7,20}")


class BookFlow(StatesGroup):
    service = State()
    master = State()
    day = State()
    slot = State()
    name = State()
    phone = State()
    confirm = State()


def build_client_router(
    storage: Storage, booking: BookingService, notifier: Notifier, settings: Settings
) -> Router:
    router = Router(name="client")
    tz = settings.tz

    # ── меню ──────────────────────────────────────────────────────────────

    @router.message(Command("start"))
    async def cmd_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        if message.from_user:
            await storage.upsert_client(
                message.from_user.id,
                message.from_user.full_name,
                username=message.from_user.username,
            )
        await message.answer(
            f"Здравствуйте! Это бот записи «{escape(settings.business_name)}».\n\n"
            "Выберите услугу, удобный день и время — я подберу свободные окна "
            "и напомню о визите заранее.",
            reply_markup=main_menu(),
        )

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(
            "<b>Что я умею</b>\n\n"
            f"{BTN_BOOK} — выбрать услугу и время\n"
            f"{BTN_MY} — посмотреть, отменить или перенести запись\n"
            f"{BTN_SERVICES} — прайс и длительность\n"
            f"{BTN_CONTACTS} — адрес и телефон\n\n"
            f"Напомню о визите за {settings.reminder_lead_min} мин.",
            reply_markup=main_menu(),
        )

    @router.message(F.text == BTN_SERVICES)
    async def show_services(message: Message) -> None:
        services = await storage.list_services()
        await message.answer(services_list(services))

    @router.message(F.text == BTN_CONTACTS)
    async def show_contacts(message: Message) -> None:
        lines = [f"<b>{escape(settings.business_name)}</b>"]
        if settings.business_address:
            lines.append(f"📍 {escape(settings.business_address)}")
        if settings.business_phone:
            lines.append(f"📞 {escape(settings.business_phone)}")
        if len(lines) == 1:
            lines.append("Контакты пока не заполнены.")
        await message.answer("\n".join(lines))

    # ── шаг 1: услуга ─────────────────────────────────────────────────────

    @router.message(F.text == BTN_BOOK)
    @router.message(Command("book"))
    async def start_booking(message: Message, state: FSMContext) -> None:
        services = await storage.list_services()
        if not services:
            await message.answer(
                "Записаться пока не на что — администратор ещё не завёл услуги."
            )
            return

        await state.clear()
        await state.update_data(mode=MODE_NEW)
        await state.set_state(BookFlow.service)
        await message.answer("Что вас интересует?", reply_markup=services_keyboard(services))

    @router.callback_query(F.data.startswith(f"{CB_SERVICE}:"))
    async def pick_service(callback: CallbackQuery, state: FSMContext) -> None:
        service = await _service_from_payload(storage, callback)
        if service is None:
            return

        await state.update_data(service_id=service.id)
        masters = await storage.list_masters()

        if len(masters) <= 1:
            # Одного мастера выбирать не из чего — лишний экран только удлиняет путь.
            await state.update_data(master_id=masters[0].id if masters else None)
            await _show_calendar(callback, state)
            return

        await state.set_state(BookFlow.master)
        await _edit(
            callback,
            f"<b>{escape(service.title)}</b> · {human_duration(service.duration_min)}\n\n"
            "К кому записать?",
            masters_keyboard(masters),
        )
        await callback.answer()

    # ── шаг 2: мастер ─────────────────────────────────────────────────────

    @router.callback_query(F.data.startswith(f"{CB_MASTER}:"))
    async def pick_master(callback: CallbackQuery, state: FSMContext) -> None:
        raw = payload(callback.data)
        master_id = int(raw) if raw.isdigit() else 0
        await state.update_data(master_id=master_id or None)
        await _show_calendar(callback, state)

    # ── шаг 3: день ───────────────────────────────────────────────────────

    @router.callback_query(F.data.startswith(f"{CB_DAY}:"))
    async def pick_day(callback: CallbackQuery, state: FSMContext) -> None:
        try:
            day = date.fromisoformat(payload(callback.data))
        except ValueError:
            await callback.answer("Не разобрал дату")
            return

        data = await state.get_data()
        service = await storage.get_service(data.get("service_id", 0))
        if service is None:
            await callback.answer("Услуга больше недоступна")
            return

        slots = await booking.free_slots(day, service, data.get("master_id"))
        if not slots:
            # За время листания календаря день мог закрыться — показываем
            # календарь заново, а не пустую сетку.
            await callback.answer("На этот день свободного времени уже нет")
            await _show_calendar(callback, state)
            return

        await state.update_data(day=day.isoformat())
        await state.set_state(BookFlow.slot)
        await _edit(
            callback,
            f"<b>{escape(service.title)}</b>\n{fmt_date(day)} — свободное время:",
            slots_keyboard(slots, tz),
        )
        await callback.answer()

    # ── шаг 4: время ──────────────────────────────────────────────────────

    @router.callback_query(F.data.startswith(f"{CB_SLOT}:"))
    async def pick_slot(callback: CallbackQuery, state: FSMContext) -> None:
        slot = slot_from_payload(payload(callback.data))
        if slot is None:
            await callback.answer("Не разобрал время")
            return

        await state.update_data(slot_ts=int(slot.timestamp()))
        client = await storage.get_client(callback.from_user.id)

        if client is None or not client.phone:
            # Телефон нужен ровно один раз: без него мастеру некуда позвонить,
            # если что-то сдвинется.
            await state.set_state(BookFlow.phone)
            await _edit(callback, "Почти готово. Оставьте телефон для связи:", None)
            await callback.message.answer(
                "Нажмите кнопку ниже или пришлите номер сообщением.",
                reply_markup=request_phone(),
            )
            await callback.answer()
            return

        await _show_confirm(callback, state)

    @router.message(StateFilter(BookFlow.phone), F.contact)
    async def got_contact(message: Message, state: FSMContext) -> None:
        contact: Contact = message.contact
        await _save_contact(message, state, contact.phone_number)

    @router.message(StateFilter(BookFlow.phone), F.text)
    async def got_phone_text(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        if not _PHONE_RE.fullmatch(raw) or sum(char.isdigit() for char in raw) < 7:
            await message.answer("Не похоже на номер. Пример: +7 900 123-45-67")
            return
        await _save_contact(message, state, raw)

    async def _save_contact(message: Message, state: FSMContext, phone: str) -> None:
        await storage.upsert_client(
            message.from_user.id,
            message.from_user.full_name,
            phone=_normalize_phone(phone),
            username=message.from_user.username,
        )
        await message.answer("Спасибо!", reply_markup=main_menu())
        await _show_confirm(message, state)

    # ── шаг 5: подтверждение ──────────────────────────────────────────────

    @router.callback_query(F.data.startswith(f"{CB_CONFIRM}:"))
    async def confirm(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        service = await storage.get_service(data.get("service_id", 0))
        slot_ts = data.get("slot_ts")
        if service is None or slot_ts is None:
            await callback.answer("Что-то потерялось, начните заново")
            await state.clear()
            return

        starts_at = datetime.fromtimestamp(int(slot_ts), UTC)

        if data.get("mode") == MODE_MOVE:
            await _do_reschedule(callback, state, data, starts_at)
            return

        try:
            created = await booking.book(
                client_id=callback.from_user.id,
                service=service,
                starts_at=starts_at,
                master_id=data.get("master_id"),
            )
        except SlotTaken:
            await callback.answer("Это время только что заняли")
            await _show_calendar(callback, state)
            return

        await state.clear()
        await _edit(
            callback,
            "✅ <b>Вы записаны</b>\n\n"
            + booking_card(created, tz)
            + f"\n\nНапомню за {settings.reminder_lead_min} мин. "
            "Отменить или перенести можно в «Мои записи».",
            None,
        )
        await callback.answer("Готово")
        await notifier.new_booking(created)

    async def _do_reschedule(
        callback: CallbackQuery, state: FSMContext, data: dict, starts_at: datetime
    ) -> None:
        current = await storage.get_booking(int(data.get("booking_id", 0)))
        if current is None or not current.is_active:
            await callback.answer("Запись уже неактивна")
            await state.clear()
            return

        was = fmt_moment(current.starts_at, tz)
        try:
            moved = await booking.reschedule(current, starts_at)
        except SlotTaken:
            await callback.answer("Это время только что заняли")
            await _show_calendar(callback, state)
            return

        await state.clear()
        await _edit(callback, "🔄 <b>Перенесли</b>\n\n" + booking_card(moved, tz), None)
        await callback.answer("Готово")
        await notifier.moved(moved, was)

    # ── мои записи ────────────────────────────────────────────────────────

    @router.message(F.text == BTN_MY)
    @router.message(Command("my"))
    async def my_bookings(message: Message) -> None:
        items = await storage.client_bookings(message.from_user.id)
        if not items:
            await message.answer(
                "Активных записей нет.", reply_markup=main_menu()
            )
            return

        for item in items:
            await message.answer(
                booking_card(item, tz),
                reply_markup=booking_keyboard(item, can_cancel=booking.can_cancel(item)),
            )

    @router.callback_query(F.data.startswith(f"{CB_CANCEL}:"))
    async def ask_cancel(callback: CallbackQuery) -> None:
        booking_id = payload(callback.data)
        if not booking_id.isdigit():
            await callback.answer()
            return
        await callback.message.answer(
            "Точно отменить запись?", reply_markup=cancel_confirm_keyboard(int(booking_id))
        )
        await callback.answer()

    @router.callback_query(F.data.startswith(f"{CB_CANCEL_YES}:"))
    async def do_cancel(callback: CallbackQuery) -> None:
        raw = payload(callback.data)
        current = await storage.get_booking(int(raw)) if raw.isdigit() else None
        if current is None or current.client_id != callback.from_user.id:
            await callback.answer("Запись не найдена")
            return
        if not booking.can_cancel(current):
            await _edit(
                callback,
                "Отменить самостоятельно уже поздно — позвоните нам, пожалуйста.",
                None,
            )
            await callback.answer()
            return

        cancelled = await storage.cancel_booking(current.id, by="client")
        if cancelled is None:
            await callback.answer("Запись уже отменена")
            return

        await _edit(callback, "❌ Запись отменена. Будем рады видеть вас в другой раз.", None)
        await callback.answer()
        await notifier.cancelled(cancelled, by_client=True)

    @router.callback_query(F.data.startswith(f"{CB_MOVE}:"))
    async def start_move(callback: CallbackQuery, state: FSMContext) -> None:
        raw = payload(callback.data)
        current = await storage.get_booking(int(raw)) if raw.isdigit() else None
        if current is None or current.client_id != callback.from_user.id or not current.is_active:
            await callback.answer("Запись не найдена")
            return

        await state.clear()
        await state.update_data(
            mode=MODE_MOVE,
            booking_id=current.id,
            service_id=current.service_id,
            master_id=current.master_id,
        )
        await _show_calendar(callback, state)

    # ── навигация ─────────────────────────────────────────────────────────

    @router.callback_query(F.data.startswith(f"{CB_BACK}:"))
    async def go_back(callback: CallbackQuery, state: FSMContext) -> None:
        step = payload(callback.data)
        if step == "service":
            services = await storage.list_services()
            await state.set_state(BookFlow.service)
            await _edit(callback, "Что вас интересует?", services_keyboard(services))
            await callback.answer()
            return
        if step == "master":
            masters = await storage.list_masters()
            if len(masters) > 1:
                await state.set_state(BookFlow.master)
                await _edit(callback, "К кому записать?", masters_keyboard(masters))
                await callback.answer()
                return
        await _show_calendar(callback, state)

    @router.callback_query(F.data.startswith(f"{CB_NOOP}:"))
    async def noop(callback: CallbackQuery) -> None:
        await callback.answer("В этот день свободного времени нет")

    # ── общее ─────────────────────────────────────────────────────────────

    async def _show_calendar(target: CallbackQuery | Message, state: FSMContext) -> None:
        data = await state.get_data()
        service = await storage.get_service(data.get("service_id", 0))
        if service is None:
            await _reply(target, "Услуга больше недоступна, выберите другую.")
            await state.clear()
            return

        days, closed = await booking.calendar(service, data.get("master_id"))
        if len(closed) == len(days):
            await _reply(
                target,
                "Свободного времени на ближайшие дни нет. "
                "Загляните позже — записи часто освобождаются.",
            )
            return

        await state.set_state(BookFlow.day)
        masters = await storage.list_masters()
        header = f"<b>{escape(service.title)}</b> · {human_duration(service.duration_min)}"
        if service.price:
            header += f" · {fmt_price(service.price)}"
        await _reply(
            target,
            f"{header}\n\nВыберите день:",
            days_keyboard(days, closed, booking.today(), with_back=len(masters) > 1),
        )
        if isinstance(target, CallbackQuery):
            await target.answer()

    async def _show_confirm(target: CallbackQuery | Message, state: FSMContext) -> None:
        data = await state.get_data()
        service = await storage.get_service(data.get("service_id", 0))
        slot_ts = data.get("slot_ts")
        if service is None or slot_ts is None:
            await _reply(target, "Что-то потерялось, начните заново.")
            await state.clear()
            return

        starts_at = datetime.fromtimestamp(int(slot_ts), UTC)
        master_id = data.get("master_id")
        master = await storage.get_master(master_id) if master_id else None

        lines = [
            "Проверьте, всё ли верно:",
            "",
            f"<b>{escape(service.title)}</b>",
            f"🗓 {fmt_moment(starts_at, tz)}",
            f"⏱ {human_duration(service.duration_min)}",
        ]
        if master:
            lines.append(f"💇 {escape(master.name)}")
        if service.price:
            lines.append(f"💰 {fmt_price(service.price)}")

        await state.set_state(BookFlow.confirm)
        await _reply(target, "\n".join(lines), confirm_keyboard())
        if isinstance(target, CallbackQuery):
            await target.answer()

    return router


# ──────────────────────────────────────────────────────────────────────────────
# Вспомогательное
# ──────────────────────────────────────────────────────────────────────────────


async def _service_from_payload(storage: Storage, callback: CallbackQuery) -> Service | None:
    raw = payload(callback.data)
    service = await storage.get_service(int(raw)) if raw.isdigit() else None
    if service is None or not service.active:
        await callback.answer("Услуга больше недоступна")
        return None
    return service


async def _edit(callback: CallbackQuery, text: str, keyboard) -> None:
    """Заменить текст сообщения, под которым нажали кнопку.

    Редактирование вместо новых сообщений: иначе после четырёх шагов выбора
    чат превращается в простыню из устаревших клавиатур.
    """
    if not isinstance(callback.message, Message):
        return
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except Exception:
        # Сообщение могло устареть (Telegram не даёт править старее 48 часов)
        # или остаться без изменений — тогда просто отправим новое.
        await callback.message.answer(text, reply_markup=keyboard)


async def _reply(target: CallbackQuery | Message, text: str, keyboard=None) -> None:
    if isinstance(target, CallbackQuery):
        await _edit(target, text, keyboard)
    else:
        await target.answer(text, reply_markup=keyboard or ReplyKeyboardRemove())


def _normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in "78":
        return f"+7 {digits[1:4]} {digits[4:7]}-{digits[7:9]}-{digits[9:]}"
    if len(digits) == 10:
        return f"+7 {digits[0:3]} {digits[3:6]}-{digits[6:8]}-{digits[8:]}"
    return raw.strip()
