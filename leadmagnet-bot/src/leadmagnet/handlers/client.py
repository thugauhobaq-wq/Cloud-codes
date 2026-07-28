"""Подписчик: пришёл по ссылке или написал кодовое слово — получил материал."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from ..codes import looks_like_code, parse_start_payload
from ..config import Settings
from ..delivery import Deliverer, is_subscribed
from ..keyboards import CB_CHECK_SUB, CB_MAGNET, magnets_keyboard, payload, subscription_keyboard
from ..models import Magnet
from ..storage import Storage
from ..texts import escape, subscription_gate, unknown_code, welcome

log = logging.getLogger(__name__)


def build_client_router(
    storage: Storage,
    deliverer: Deliverer,
    settings: Settings,
    notify_owner=None,
) -> Router:
    router = Router(name="client")

    # ── вход ──────────────────────────────────────────────────────────────

    @router.message(Command("start"))
    async def cmd_start(message: Message, command: CommandObject) -> None:
        code, source = parse_start_payload(command.args or "")
        lead = await storage.get_lead(message.from_user.id)
        is_new = lead is None

        await storage.upsert_lead(
            message.from_user.id,
            name=message.from_user.full_name,
            username=message.from_user.username,
            source=source,
        )
        if is_new and notify_owner is not None and settings.notify_new_leads:
            label = f" (источник: {source})" if source else ""
            await notify_owner(f"👤 Новый подписчик: {escape(message.from_user.full_name)}{label}")

        if code:
            # Пришёл по ссылке из поста: материал должен прийти сразу, без
            # лишнего шага «а теперь напишите слово».
            magnet = await storage.magnet_by_code(code)
            if magnet is not None and magnet.ready:
                await _hand_over(message, magnet, source)
                return

        magnets = await storage.list_magnets()
        await message.answer(
            welcome(settings, magnets), reply_markup=magnets_keyboard(magnets)
        )

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        magnets = await storage.list_magnets()
        await message.answer(
            welcome(settings, magnets), reply_markup=magnets_keyboard(magnets)
        )

    @router.message(Command("my"))
    async def cmd_my(message: Message) -> None:
        items = await storage.lead_deliveries(message.from_user.id)
        if not items:
            await message.answer("Вы ещё ничего не получали. Напишите кодовое слово из поста.")
            return
        lines = ["<b>Что вы уже получили</b>", ""]
        lines.extend(
            f"• {escape(item.magnet_title)} — <code>{escape(item.magnet_code)}</code>"
            for item in items
        )
        lines.append("")
        lines.append("Напишите кодовое слово ещё раз, если файл потерялся.")
        await message.answer("\n".join(lines))

    # ── кодовое слово ─────────────────────────────────────────────────────

    @router.message(F.text)
    async def maybe_code(message: Message) -> None:
        text = message.text or ""
        await storage.upsert_lead(
            message.from_user.id,
            name=message.from_user.full_name,
            username=message.from_user.username,
        )

        if not looks_like_code(text):
            # Длинное сообщение — это разговор, а не кодовое слово. Отвечать на
            # него «не знаю такого слова» невежливо.
            await message.answer(
                "Напишите кодовое слово из поста — пришлю материал. "
                "Список слов: /help"
            )
            return

        magnet = await storage.magnet_by_code(text)
        if magnet is None or not magnet.ready:
            magnets = await storage.list_magnets()
            await message.answer(unknown_code(magnets), reply_markup=magnets_keyboard(magnets))
            return

        lead = await storage.get_lead(message.from_user.id)
        await _hand_over(message, magnet, lead.source if lead else "")

    @router.callback_query(F.data.startswith(f"{CB_MAGNET}:"))
    async def pick_magnet(callback: CallbackQuery) -> None:
        raw = payload(callback.data)
        magnet = await storage.get_magnet(int(raw)) if raw.isdigit() else None
        if magnet is None or not magnet.ready or not magnet.active:
            await callback.answer("Материал недоступен")
            return

        lead = await storage.get_lead(callback.from_user.id)
        await callback.answer()
        await _hand_over(
            callback.message, magnet, lead.source if lead else "", user=callback.from_user
        )

    # ── проверка подписки ─────────────────────────────────────────────────

    @router.callback_query(F.data.startswith(f"{CB_CHECK_SUB}:"))
    async def recheck_subscription(callback: CallbackQuery, bot: Bot) -> None:
        raw = payload(callback.data)
        magnet = await storage.get_magnet(int(raw)) if raw.isdigit() else None
        if magnet is None or not magnet.ready:
            await callback.answer("Материал недоступен")
            return

        if not await is_subscribed(bot, settings.required_channel, callback.from_user.id):
            await callback.answer(
                "Пока не вижу подписки. Подпишитесь и нажмите ещё раз.", show_alert=True
            )
            return

        await callback.answer("Спасибо! Отправляю")
        lead = await storage.get_lead(callback.from_user.id)
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                log.debug("не убрал клавиатуру", exc_info=True)
        await deliverer.deliver(
            callback.from_user.id, magnet, lead.source if lead else ""
        )

    # ── общее ─────────────────────────────────────────────────────────────

    async def _hand_over(message: Message, magnet: Magnet, source: str, user=None) -> None:
        """Выдать материал или попросить подписаться на канал."""
        user = user or message.from_user
        if settings.check_subscription and not await is_subscribed(
            message.bot, settings.required_channel, user.id
        ):
            await message.answer(
                subscription_gate(settings, magnet),
                reply_markup=subscription_keyboard(settings, magnet),
            )
            return

        delivered = await deliverer.deliver(user.id, magnet, source)
        if not delivered:
            await message.answer(
                "Не получилось отправить материал. Напишите ещё раз чуть позже."
            )

    return router
