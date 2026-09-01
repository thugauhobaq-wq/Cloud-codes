"""Организатор: создать розыгрыш, следить, подвести итоги, перевыбрать."""

from __future__ import annotations

import csv
import io
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, BufferedInputFile, CallbackQuery, Message
from botkit import IsAdmin
from botkit.messaging import edit, parts, payload
from botkit.texts import counted, escape

from ..config import Settings
from ..keyboards import (
    CB_CANCEL,
    CB_FINISH,
    CB_PARTICIPANTS,
    CB_REROLL,
    admin_keyboard,
    reroll_keyboard,
)
from ..notify import Notify
from ..parsing import ParseError, parse_new
from ..storage import AlreadyFinished, NoParticipants, Storage
from ..texts import giveaway_card, giveaway_line, post_for_channel, proof

log = logging.getLogger(__name__)

ADMIN_HELP = """<b>Розыгрыши</b>

<b>Создать</b>
/new Кофе в подарок | Пачка зёрен 250 г | 2 | 3д | @mychannel
   название | приз | победителей | срок | каналы для подписки
Обязательно только название. Срок: 3д, 48ч, 30м, 5.09 18:00 — без него
итоги подводятся вручную.

<b>Вести</b>
/giveaways — список с кнопками
/g 3 — карточка розыгрыша
/post 3 — готовый текст поста для канала
/participants 3 — сколько собралось и последние
/export 3 — участники в CSV

<b>Итоги</b>
/finish 3 — подвести досрочно
/winners 3 — победители и замена не откликнувшихся
/proof 3 — расчёт жребия по шагам
/cancel 3 — отменить розыгрыш"""


def build_admin_router(storage: Storage, notify: Notify, settings: Settings) -> Router:
    router = Router(name="admin")

    router.message.filter(IsAdmin(settings.admins()))
    router.callback_query.filter(IsAdmin(settings.admins()))

    @router.message(Command("admin", "help"))
    async def cmd_admin(message: Message) -> None:
        await message.answer(ADMIN_HELP)

    # ── создание ──────────────────────────────────────────────────────────

    @router.message(Command("new"))
    async def cmd_new(message: Message, command: CommandObject) -> None:
        try:
            spec = parse_new(command.args or "")
        except ParseError as exc:
            await message.answer(str(exc))
            return

        channels = spec.channels or settings.channels()
        giveaway = await storage.create_giveaway(
            spec.title,
            prize=spec.prize,
            winners_count=spec.winners_count,
            ends_at=spec.ends_at,
            channels=channels,
        )

        await message.answer(
            giveaway_card(giveaway, settings, for_admin=True),
            reply_markup=admin_keyboard(giveaway),
        )
        await message.answer(
            "Текст для канала — скопируйте вместе со ссылкой:\n\n"
            + post_for_channel(giveaway, settings),
            disable_web_page_preview=True,
        )
        if channels and not settings.announce_chat:
            await message.answer(
                "⚠️ Подписку проверяю, но публиковать итоги некуда: "
                "укажите ANNOUNCE_CHAT в .env, иначе объявление придёт вам сюда."
            )

    # ── ведение ───────────────────────────────────────────────────────────

    @router.message(Command("giveaways"))
    async def cmd_list(message: Message) -> None:
        items = await storage.list_giveaways()
        if not items:
            await message.answer("Розыгрышей пока нет. Создать: /new Название | приз | 1 | 3д")
            return

        lines = ["<b>Розыгрыши</b>", ""]
        lines.extend(giveaway_line(item) for item in items)
        lines.append("")
        lines.append("Открыть: /g 3")
        await message.answer("\n".join(lines))

    @router.message(Command("g"))
    async def cmd_show(message: Message, command: CommandObject) -> None:
        giveaway = await _by_id(storage, command.args)
        if giveaway is None:
            await message.answer("Не нашёл. Формат: /g 3")
            return
        await message.answer(
            giveaway_card(giveaway, settings, for_admin=True),
            reply_markup=admin_keyboard(giveaway),
        )

    @router.message(Command("post"))
    async def cmd_post(message: Message, command: CommandObject) -> None:
        giveaway = await _by_id(storage, command.args)
        if giveaway is None:
            await message.answer("Не нашёл. Формат: /post 3")
            return
        await message.answer(post_for_channel(giveaway, settings), disable_web_page_preview=True)

    @router.message(Command("participants"))
    async def cmd_participants(message: Message, command: CommandObject) -> None:
        giveaway = await _by_id(storage, command.args)
        if giveaway is None:
            await message.answer("Не нашёл. Формат: /participants 3")
            return
        await _show_participants(message, giveaway.id, storage)

    @router.callback_query(F.data.startswith(f"{CB_PARTICIPANTS}:"))
    async def show_participants(callback: CallbackQuery) -> None:
        raw = payload(callback.data)
        if not raw.isdigit():
            await callback.answer()
            return
        await callback.answer()
        await _show_participants(callback.message, int(raw), storage)

    @router.message(Command("export"))
    async def cmd_export(message: Message, command: CommandObject) -> None:
        giveaway = await _by_id(storage, command.args)
        if giveaway is None:
            await message.answer("Не нашёл. Формат: /export 3")
            return

        rows = await storage.export_rows(giveaway.id)
        if not rows:
            await message.answer("Участников пока нет.")
            return

        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";")
        writer.writerow(["Telegram ID", "Имя", "Username", "Присоединился", "Место"])
        for tg_id, name, username, joined_at, place in rows:
            writer.writerow(
                [
                    tg_id,
                    name,
                    f"@{username}" if username else "",
                    (joined_at or "")[:19].replace("T", " "),
                    place,
                ]
            )

        # utf-8-sig — чтобы Excel не показал кириллицу кракозябрами.
        await message.answer_document(
            BufferedInputFile(
                buffer.getvalue().encode("utf-8-sig"), filename=f"giveaway-{giveaway.id}.csv"
            ),
            caption=f"Участников: {len(rows)}",
        )

    # ── итоги ─────────────────────────────────────────────────────────────

    @router.message(Command("finish"))
    async def cmd_finish(message: Message, command: CommandObject) -> None:
        giveaway = await _by_id(storage, command.args)
        if giveaway is None:
            await message.answer("Не нашёл. Формат: /finish 3")
            return
        await _finish(message, giveaway.id, storage, notify, settings)

    @router.callback_query(F.data.startswith(f"{CB_FINISH}:"))
    async def finish_button(callback: CallbackQuery) -> None:
        raw = payload(callback.data)
        if not raw.isdigit():
            await callback.answer()
            return
        await callback.answer("Подвожу итоги")
        await _finish(callback.message, int(raw), storage, notify, settings)

    @router.message(Command("cancel"))
    async def cmd_cancel(message: Message, command: CommandObject) -> None:
        giveaway = await _by_id(storage, command.args)
        if giveaway is None:
            await message.answer("Не нашёл. Формат: /cancel 3")
            return
        cancelled = await storage.cancel_giveaway(giveaway.id)
        await message.answer(
            f"Розыгрыш #{giveaway.id} отменён." if cancelled else "Отменять нечего: уже не идёт."
        )

    @router.callback_query(F.data.startswith(f"{CB_CANCEL}:"))
    async def cancel_button(callback: CallbackQuery) -> None:
        raw = payload(callback.data)
        cancelled = await storage.cancel_giveaway(int(raw)) if raw.isdigit() else None
        await callback.answer("Отменил" if cancelled else "Уже не идёт")
        if cancelled:
            await edit(callback, giveaway_card(cancelled, settings, for_admin=True), None)

    @router.message(Command("winners"))
    async def cmd_winners(message: Message, command: CommandObject) -> None:
        giveaway = await _by_id(storage, command.args)
        if giveaway is None:
            await message.answer("Не нашёл. Формат: /winners 3")
            return

        winners = await storage.winners(giveaway.id)
        if not winners:
            await message.answer("Победителей ещё нет — итоги не подведены.")
            return

        lines = [f"<b>Победители розыгрыша #{giveaway.id}</b>", ""]
        for item in winners:
            who = f"@{item.username}" if item.username else (item.name or str(item.tg_id))
            mark = "" if item.notified else " · не доставлено"
            again = f" · замена #{item.round_number}" if item.round_number else ""
            lines.append(f"{item.place}. {escape(who)} ({item.tg_id}){mark}{again}")
        lines.append("")
        lines.append("Если победитель не откликнулся — можно перевыбрать:")

        await message.answer(
            "\n".join(lines), reply_markup=reroll_keyboard(giveaway.id, [w.place for w in winners])
        )

    @router.callback_query(F.data.startswith(f"{CB_REROLL}:"))
    async def reroll(callback: CallbackQuery) -> None:
        chunks = parts(callback.data)
        if len(chunks) < 3 or not chunks[1].isdigit() or not chunks[2].isdigit():
            await callback.answer()
            return

        giveaway_id, place = int(chunks[1]), int(chunks[2])
        winner = await storage.reroll(giveaway_id, place)
        if winner is None:
            await callback.answer("Некем заменить: все участники уже победили", show_alert=True)
            return

        giveaway = await storage.get_giveaway(giveaway_id)
        await callback.answer("Перевыбрал")
        who = f"@{winner.username}" if winner.username else (winner.name or str(winner.tg_id))
        await callback.message.answer(
            f"🔁 На {place}-м месте теперь {escape(who)}.\n"
            f"Зерно замены: <code>{escape(winner.seed)}</code>"
        )
        if giveaway is not None:
            await notify.notify(winner.tg_id, f"🎉 Вы выиграли в розыгрыше «{giveaway.title}»!")
            await storage.mark_notified(giveaway_id, winner.tg_id)

    @router.message(Command("proof"))
    async def cmd_proof(message: Message, command: CommandObject) -> None:
        giveaway = await _by_id(storage, command.args)
        if giveaway is None or not giveaway.seed:
            await message.answer("Итоги не подведены — проверять нечего. Формат: /proof 3")
            return
        await message.answer(
            proof(
                giveaway,
                await storage.participant_ids(giveaway.id),
                await storage.winners(giveaway.id),
            )
        )

    @router.message(Command("stats"))
    async def cmd_stats(message: Message) -> None:
        stats = await storage.stats()
        await message.answer(
            "<b>Всего</b>\n\n"
            f"Розыгрышей: {stats['giveaways']} (идёт: {stats['active']})\n"
            f"Участий: {stats['participants']}\n"
            f"Разных людей: {stats['people']}"
        )

    return router


# ──────────────────────────────────────────────────────────────────────────────


async def _by_id(storage: Storage, args: str | None):
    raw = (args or "").strip().lstrip("#g")
    return await storage.get_giveaway(int(raw)) if raw.isdigit() else None


async def _show_participants(message: Message, giveaway_id: int, storage: Storage) -> None:
    total = await storage.count_participants(giveaway_id)
    recent = await storage.last_participants(giveaway_id)

    lines = [f"<b>Участников: {total}</b>", ""]
    if recent:
        lines.append("Последние:")
        for item in recent:
            who = f"@{item.username}" if item.username else (item.name or str(item.tg_id))
            lines.append(f"• {escape(who)}")
    else:
        lines.append("Пока никого.")
    await message.answer("\n".join(lines))


async def _finish(
    message: Message, giveaway_id: int, storage: Storage, notify: Notify, settings: Settings
) -> None:
    try:
        winners = await storage.finish(giveaway_id)
    except NoParticipants:
        await storage.cancel_giveaway(giveaway_id)
        await message.answer("Участников не было — розыгрыш закрыт без победителей.")
        return
    except AlreadyFinished:
        await message.answer("Итоги уже подведены.")
        return

    giveaway = await storage.get_giveaway(giveaway_id)
    if giveaway is None:
        return

    published = await notify.announce(giveaway, winners)
    unreachable = await notify.tell_winners(giveaway, winners)
    for winner in winners:
        if winner not in unreachable:
            await storage.mark_notified(giveaway.id, winner.tg_id)

    tail = (
        "Объявление опубликовано в канале."
        if published
        else "Объявление — выше, опубликуйте его сами."
    )
    note = ""
    if unreachable:
        note = (
            f"\n⚠️ {counted(len(unreachable), 'победитель', 'победителя', 'победителей')} "
            "заблокировали бота — их можно перевыбрать: /winners " + str(giveaway.id)
        )
    await message.answer(f"🏁 Итоги подведены. {tail}{note}")


def admin_commands() -> list[BotCommand]:
    return [
        BotCommand(command="new", description="Новый розыгрыш"),
        BotCommand(command="giveaways", description="Список розыгрышей"),
        BotCommand(command="finish", description="Подвести итоги"),
        BotCommand(command="winners", description="Победители"),
        BotCommand(command="admin", description="Справка"),
    ]
