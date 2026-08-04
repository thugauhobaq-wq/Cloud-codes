"""Админка воронки: магниты, ссылки, база подписчиков, рассылки, статистика."""

from __future__ import annotations

import csv
import io
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    Message,
)

from ..broadcast import Broadcaster, Progress
from ..codes import normalize, normalize_source
from ..config import Settings
from ..keyboards import (
    CB_BROADCAST_GO,
    CB_BROADCAST_NO,
    CB_MAGNET_TOGGLE,
    admin_magnets_keyboard,
    broadcast_confirm_keyboard,
    payload,
)
from ..models import KIND_FILE, KIND_PHOTO, KIND_TEXT
from ..storage import DuplicateCode, Storage
from ..texts import (
    broadcast_report,
    escape,
    lead_line,
    magnet_links,
    magnets_list,
    stats_text,
)

log = logging.getLogger(__name__)

ADMIN_HELP = """<b>Админка воронки</b>

<b>Материалы</b>
/magnets — список, нажатие включает и выключает
/magnet_add чеклист | Чек-лист по запуску | текст материала
   кодовое слово | название | текст (текст можно добавить позже файлом)
/magnet_file чеклист — пришлите файл или картинку с этой подписью
/followup чеклист | Как дела с чек-листом? Отвечу на вопросы.
/magnet_del чеклист
/link чеклист instagram vk — ссылки с метками источника

<b>База</b>
/leads — сколько собрано и последние пришедшие
/export — выгрузка базы в CSV
/stats — воронка за 30 дней, /stats 7 — за неделю

<b>Рассылка</b>
/broadcast текст — всем подписчикам
/broadcast_to чеклист текст — только получившим этот материал
/broadcasts — последние рассылки"""


def build_admin_router(
    storage: Storage, broadcaster: Broadcaster, settings: Settings
) -> Router:
    router = Router(name="admin")
    admins = settings.admins()

    router.message.filter(F.from_user.id.in_(admins))
    router.callback_query.filter(F.from_user.id.in_(admins))

    @router.message(Command("admin"))
    async def cmd_admin(message: Message) -> None:
        await message.answer(ADMIN_HELP)

    # ── материалы ─────────────────────────────────────────────────────────

    @router.message(Command("magnets"))
    async def cmd_magnets(message: Message) -> None:
        magnets = await storage.list_magnets(only_active=False)
        await message.answer(
            magnets_list(magnets, settings),
            reply_markup=admin_magnets_keyboard(magnets) if magnets else None,
        )

    @router.callback_query(F.data.startswith(f"{CB_MAGNET_TOGGLE}:"))
    async def toggle_magnet(callback: CallbackQuery) -> None:
        raw = payload(callback.data)
        magnet = await storage.get_magnet(int(raw)) if raw.isdigit() else None
        if magnet is None:
            await callback.answer("Материал не найден")
            return

        await storage.update_magnet(magnet.id, active=int(not magnet.active))
        await callback.answer("Выключил" if magnet.active else "Включил")
        magnets = await storage.list_magnets(only_active=False)
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                magnets_list(magnets, settings),
                reply_markup=admin_magnets_keyboard(magnets),
            )

    @router.message(Command("magnet_add"))
    async def cmd_magnet_add(message: Message, command: CommandObject) -> None:
        parts = [part.strip() for part in (command.args or "").split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            await message.answer(
                "Формат: /magnet_add чеклист | Чек-лист по запуску | текст материала\n"
                "Текст можно не указывать, а потом прислать файл: /magnet_file чеклист"
            )
            return

        body = parts[2] if len(parts) > 2 else ""
        try:
            magnet = await storage.add_magnet(
                parts[0], parts[1], kind=KIND_TEXT, body=body
            )
        except DuplicateCode:
            await message.answer(
                f"Кодовое слово <code>{escape(normalize(parts[0]))}</code> уже занято."
            )
            return
        except ValueError:
            await message.answer("Кодовое слово пустое или состоит из одних знаков.")
            return

        note = "" if magnet.ready else "\n\n⚠️ Нечего выдавать — пришлите файл или добавьте текст."
        await message.answer(
            f"Готово: <code>{escape(magnet.code)}</code> — {escape(magnet.title)}\n"
            f"Ссылка: <code>{escape(settings.start_link(magnet.code))}</code>{note}"
        )

    @router.message(F.document | F.photo, Command("magnet_file"))
    async def cmd_magnet_file(message: Message, command: CommandObject) -> None:
        magnet = await storage.magnet_by_code((command.args or ""), only_active=False)
        if magnet is None:
            await message.answer("Не нашёл материал с таким кодовым словом.")
            return

        if message.document:
            kind, file_id = KIND_FILE, message.document.file_id
        else:
            # Из лесенки размеров берём самый крупный.
            kind, file_id = KIND_PHOTO, message.photo[-1].file_id

        await storage.update_magnet(magnet.id, kind=kind, file_id=file_id)
        await message.answer(
            f"Файл для <code>{escape(magnet.code)}</code> сохранил. "
            "Проверьте: напишите кодовое слово сами."
        )

    @router.message(Command("followup"))
    async def cmd_followup(message: Message, command: CommandObject) -> None:
        code, _, text = (command.args or "").partition("|")
        magnet = await storage.magnet_by_code(code, only_active=False)
        if magnet is None:
            await message.answer(
                "Формат: /followup чеклист | текст через сутки после выдачи"
            )
            return

        await storage.update_magnet(magnet.id, followup=text.strip())
        if text.strip():
            await message.answer(
                f"Догоняющее для <code>{escape(magnet.code)}</code> отправится через "
                f"{settings.followup_hours} ч после выдачи."
            )
        else:
            await message.answer(f"Догоняющее для <code>{escape(magnet.code)}</code> убрал.")

    @router.message(Command("magnet_del"))
    async def cmd_magnet_del(message: Message, command: CommandObject) -> None:
        magnet = await storage.magnet_by_code((command.args or ""), only_active=False)
        if magnet is None:
            await message.answer("Формат: /magnet_del чеклист")
            return
        await storage.delete_magnet(magnet.id)
        await message.answer(
            f"Удалил <code>{escape(magnet.code)}</code>. "
            "Подписчики, которые его получали, остались в базе."
        )

    @router.message(Command("link"))
    async def cmd_link(message: Message, command: CommandObject) -> None:
        args = (command.args or "").split()
        if not args:
            await message.answer("Формат: /link чеклист instagram vk")
            return

        magnet = await storage.magnet_by_code(args[0], only_active=False)
        if magnet is None:
            await message.answer("Не нашёл материал с таким кодовым словом.")
            return

        sources = [normalize_source(item) for item in args[1:]]
        await message.answer(magnet_links(magnet, settings, [item for item in sources if item]))

    # ── база ──────────────────────────────────────────────────────────────

    @router.message(Command("leads"))
    async def cmd_leads(message: Message) -> None:
        total = await storage.count_leads()
        recent = await storage.list_leads(limit=10)
        lines = [f"<b>В базе: {total}</b>", ""]
        if recent:
            lines.append("Последние:")
            lines.extend(f"• {lead_line(item)}" for item in reversed(recent[-10:]))
        else:
            lines.append("Пока пусто.")
        lines.append("")
        lines.append("Выгрузка: /export")
        await message.answer("\n".join(lines))

    @router.message(Command("export"))
    async def cmd_export(message: Message) -> None:
        rows = await storage.export_rows()
        if not rows:
            await message.answer("Выгружать пока нечего.")
            return

        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";")
        writer.writerow(
            ["Telegram ID", "Имя", "Username", "Источник", "Пришёл", "Заблокировал", "Материалы"]
        )
        for row in rows:
            tg_id, name, username, source, created_at, blocked, codes = row
            writer.writerow(
                [
                    tg_id,
                    name,
                    f"@{username}" if username else "",
                    source,
                    (created_at or "")[:19].replace("T", " "),
                    "да" if blocked else "",
                    codes,
                ]
            )

        # utf-8-sig — чтобы Excel не показал кириллицу кракозябрами.
        await message.answer_document(
            BufferedInputFile(buffer.getvalue().encode("utf-8-sig"), filename="leads.csv"),
            caption=f"Подписчиков в выгрузке: {len(rows)}",
        )

    @router.message(Command("stats"))
    async def cmd_stats(message: Message, command: CommandObject) -> None:
        raw = (command.args or "").strip()
        days = int(raw) if raw.isdigit() and 0 < int(raw) <= 365 else 30

        await message.answer(
            stats_text(
                await storage.stats(days),
                await storage.by_magnet(days),
                await storage.by_source(days),
                await storage.daily(min(days, 14)),
                days,
            )
        )

    # ── рассылка ──────────────────────────────────────────────────────────

    @router.message(Command("broadcast"))
    async def cmd_broadcast(message: Message, command: CommandObject, state: FSMContext) -> None:
        text = (command.args or "").strip()
        if not text:
            await message.answer("Формат: /broadcast текст сообщения")
            return
        await _prepare_broadcast(message, state, text, magnet_code="")

    @router.message(Command("broadcast_to"))
    async def cmd_broadcast_to(
        message: Message, command: CommandObject, state: FSMContext
    ) -> None:
        parts = (command.args or "").strip().split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Формат: /broadcast_to чеклист текст сообщения")
            return

        magnet = await storage.magnet_by_code(parts[0], only_active=False)
        if magnet is None:
            await message.answer("Не нашёл материал с таким кодовым словом.")
            return
        await _prepare_broadcast(message, state, parts[1], magnet_code=magnet.code)

    async def _prepare_broadcast(
        message: Message, state: FSMContext, text: str, *, magnet_code: str
    ) -> None:
        """Показать предпросмотр и спросить подтверждение.

        Рассылка необратима: отправленное не отзовёшь, а опечатку увидит вся
        база. Один лишний вопрос здесь дешевле извинений потом.
        """
        audience = await broadcaster.audience(magnet_code)
        if not audience:
            await message.answer("Некому отправлять: в этом сегменте никого нет.")
            return

        await state.update_data(broadcast_text=text, broadcast_code=magnet_code)
        segment = f"получившим «{escape(magnet_code)}»" if magnet_code else "всей базе"
        await message.answer(
            f"<b>Предпросмотр</b>\n\n{text}\n\n"
            f"Отправлю {segment}: <b>{len(audience)}</b> чел.",
            reply_markup=broadcast_confirm_keyboard(len(audience)),
        )

    @router.callback_query(F.data.startswith(f"{CB_BROADCAST_NO}:"))
    async def cancel_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(broadcast_text="", broadcast_code="")
        await callback.answer("Отменил")
        if isinstance(callback.message, Message):
            await callback.message.edit_text("Рассылка отменена.")

    @router.callback_query(F.data.startswith(f"{CB_BROADCAST_GO}:"))
    async def start_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        text = data.get("broadcast_text", "")
        if not text:
            await callback.answer("Текст потерялся, наберите команду заново")
            return

        await state.update_data(broadcast_text="", broadcast_code="")
        await callback.answer("Начинаю")
        status = callback.message
        if isinstance(status, Message):
            await status.edit_text("📨 Рассылаю…")

        async def report(progress: Progress) -> None:
            if not isinstance(status, Message):
                return
            try:
                await status.edit_text(
                    f"📨 Рассылаю…\n\n"
                    f"Отправлено: {progress.sent} из {progress.total}\n"
                    f"Заблокировали бота: {progress.blocked}"
                )
            except Exception:
                # Текст мог не измениться между обновлениями — это не ошибка.
                log.debug("прогресс не обновился", exc_info=True)

        record = await broadcaster.run(
            text, magnet_code=data.get("broadcast_code", ""), on_progress=report
        )
        await callback.bot.send_message(callback.from_user.id, broadcast_report(record))

    @router.message(Command("broadcasts"))
    async def cmd_broadcasts(message: Message) -> None:
        items = await storage.last_broadcasts()
        if not items:
            await message.answer("Рассылок ещё не было.")
            return

        lines = ["<b>Последние рассылки</b>", ""]
        for item in items:
            when = item.created_at.strftime("%d.%m %H:%M") if item.created_at else ""
            segment = f" · {item.magnet_code}" if item.magnet_code else ""
            lines.append(
                f"#{item.id} · {when}{segment} — {item.sent} из {item.total}"
                f"{f', ошибок {item.failed}' if item.failed else ''}"
            )
            lines.append(f"    <i>{escape(item.text[:60])}…</i>")
        await message.answer("\n".join(lines))

    return router


def admin_commands() -> list[BotCommand]:
    return [
        BotCommand(command="magnets", description="Материалы"),
        BotCommand(command="leads", description="База подписчиков"),
        BotCommand(command="stats", description="Статистика воронки"),
        BotCommand(command="broadcast", description="Рассылка"),
        BotCommand(command="export", description="Выгрузка базы"),
        BotCommand(command="admin", description="Справка админа"),
    ]
