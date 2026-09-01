"""Участник: пришёл по ссылке, подписался, участвует."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from botkit.messaging import edit, payload, send_to_user

from ..config import Settings
from ..keyboards import CB_CHECK, CB_JOIN, join_keyboard, subscribe_keyboard
from ..models import Giveaway
from ..storage import AlreadyFinished, Storage
from ..subscription import missing_channels
from ..texts import giveaway_card, giveaway_line, join_confirmation, subscription_gate

log = logging.getLogger(__name__)


def build_client_router(storage: Storage, settings: Settings) -> Router:
    router = Router(name="client")

    @router.message(Command("start"))
    async def cmd_start(message: Message, command: CommandObject, bot: Bot) -> None:
        giveaway = await _from_payload(storage, command.args)

        if giveaway is None:
            active = await storage.list_giveaways(statuses=["active"], limit=5)
            if not active:
                await message.answer(
                    f"<b>{settings.project_name}</b>\n\n"
                    "Сейчас розыгрышей нет. Загляните позже — или подпишитесь "
                    "на канал организатора, там объявляют о новых."
                )
                return
            if len(active) == 1:
                await _show(message, active[0], bot, storage, settings)
                return

            lines = ["<b>Идут розыгрыши</b>", ""]
            lines.extend(giveaway_line(item) for item in active)
            lines.append("")
            lines.append("Открыть: ссылка из поста в канале или /go 3")
            await message.answer("\n".join(lines))
            return

        await _show(message, giveaway, bot, storage, settings)

    @router.message(Command("go"))
    async def cmd_go(message: Message, command: CommandObject, bot: Bot) -> None:
        raw = (command.args or "").strip().lstrip("#g")
        giveaway = await storage.get_giveaway(int(raw)) if raw.isdigit() else None
        if giveaway is None:
            await message.answer("Не нашёл такой розыгрыш. Формат: /go 3")
            return
        await _show(message, giveaway, bot, storage, settings)

    @router.message(Command("my"))
    async def cmd_my(message: Message) -> None:
        items = await storage.giveaways_of(message.from_user.id)
        if not items:
            await message.answer("Вы пока ни в чём не участвуете.")
            return

        lines = ["<b>Ваши розыгрыши</b>", ""]
        for item in items:
            winners = await storage.winners(item.id)
            won = any(item.tg_id == message.from_user.id for item in winners)
            mark = " 🎉 вы выиграли" if won else ""
            lines.append(giveaway_line(item) + mark)
        await message.answer("\n".join(lines))

    # ── участие ───────────────────────────────────────────────────────────

    @router.callback_query(F.data.startswith(f"{CB_JOIN}:"))
    async def join(callback: CallbackQuery, bot: Bot) -> None:
        await _try_join(callback, bot, storage, settings)

    @router.callback_query(F.data.startswith(f"{CB_CHECK}:"))
    async def recheck(callback: CallbackQuery, bot: Bot) -> None:
        await _try_join(callback, bot, storage, settings, rechecking=True)

    return router


# ──────────────────────────────────────────────────────────────────────────────


async def _from_payload(storage: Storage, args: str | None) -> Giveaway | None:
    """Розыгрыш из deep-link `?start=g12`."""
    raw = (args or "").strip().lower().removeprefix("g")
    if not raw.isdigit():
        return None
    return await storage.get_giveaway(int(raw))


async def _show(
    message: Message, giveaway: Giveaway, bot: Bot, storage: Storage, settings: Settings
) -> None:
    """Карточка розыгрыша с кнопкой участия — или объяснение, почему нельзя."""
    if not giveaway.is_active:
        winners = await storage.winners(giveaway.id)
        text = giveaway_card(giveaway, settings)
        if winners:
            you = any(item.tg_id == message.from_user.id for item in winners)
            text += "\n\n🎉 Вы в числе победителей!" if you else "\n\nПобедители уже определены."
        await message.answer(text)
        return

    participant = await storage.get_participant(giveaway.id, message.from_user.id)
    if participant is not None:
        await message.answer(join_confirmation(giveaway, participant))
        return

    await message.answer(giveaway_card(giveaway, settings), reply_markup=join_keyboard(giveaway))


async def _try_join(
    callback: CallbackQuery,
    bot: Bot,
    storage: Storage,
    settings: Settings,
    *,
    rechecking: bool = False,
) -> None:
    raw = payload(callback.data)
    giveaway = await storage.get_giveaway(int(raw)) if raw.isdigit() else None

    if giveaway is None:
        await callback.answer("Розыгрыш не найден")
        return
    if not giveaway.is_active:
        await callback.answer("Розыгрыш уже завершён", show_alert=True)
        return

    missing = await missing_channels(bot, giveaway.channels, callback.from_user.id)
    if missing:
        if rechecking:
            await callback.answer(
                "Пока не вижу подписки — подпишитесь и нажмите ещё раз", show_alert=True
            )
        else:
            await callback.answer()
        await edit(
            callback,
            subscription_gate(giveaway, missing),
            subscribe_keyboard(giveaway, missing),
        )
        return

    try:
        participant, already = await storage.join(
            giveaway.id,
            callback.from_user.id,
            name=callback.from_user.full_name,
            username=callback.from_user.username,
        )
    except AlreadyFinished:
        # Итоги подвели, пока человек подписывался на канал.
        await callback.answer("Розыгрыш только что завершился", show_alert=True)
        return
    await callback.answer("Вы уже участвуете" if already else "Готово!")

    fresh = await storage.get_giveaway(giveaway.id) or giveaway
    if not await edit(callback, join_confirmation(fresh, participant), None):
        # Сообщение могло устареть или прийти из канала — пишем в личку.
        await send_to_user(callback, join_confirmation(fresh, participant))
