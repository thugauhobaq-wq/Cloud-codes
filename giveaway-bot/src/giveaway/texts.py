"""Сообщения бота: карточка розыгрыша, объявление итогов, служебное."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from botkit.texts import counted, escape, short

from .config import Settings
from .draw import ticket
from .models import STATUS_TITLES, Giveaway, Participant, Winner

MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)  # fmt: skip


def fmt_deadline(moment: datetime | None) -> str:
    if moment is None:
        return "без срока — итоги подведёт организатор"
    point = moment.astimezone(UTC)
    return f"{point.day} {MONTHS[point.month - 1]}, {point:%H:%M} UTC"


def human_left(delta) -> str:
    """«2 дня», «3 часа», «12 минут» — сколько осталось до итогов."""
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "вот-вот"
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return counted(days, "день", "дня", "дней")
    if hours:
        return counted(hours, "час", "часа", "часов")
    return counted(max(1, minutes), "минута", "минуты", "минут")


def giveaway_card(giveaway: Giveaway, settings: Settings, *, for_admin: bool = False) -> str:
    lines = [f"🎁 <b>{escape(giveaway.title)}</b>"]
    if giveaway.prize:
        lines.append(escape(giveaway.prize))
    lines.append("")
    lines.append(f"Победителей: {giveaway.winners_count}")
    lines.append(f"Участников: {giveaway.participants}")

    if giveaway.is_active:
        lines.append(f"Итоги: {fmt_deadline(giveaway.ends_at)}")
        if giveaway.ends_at:
            lines.append(f"Осталось: {human_left(giveaway.time_left())}")
    else:
        lines.append(f"Статус: {STATUS_TITLES.get(giveaway.status, giveaway.status)}")

    if giveaway.channels:
        lines.append("")
        lines.append("Условие: подписка на " + ", ".join(escape(c) for c in giveaway.channels))

    if for_admin:
        lines.append("")
        lines.append(f"Номер: {giveaway.id} · ссылка: {settings.start_link(giveaway.id)}")
        if giveaway.seed:
            lines.append(f"Зерно жребия: <code>{escape(giveaway.seed)}</code>")
    return "\n".join(lines)


def giveaway_line(giveaway: Giveaway) -> str:
    status = STATUS_TITLES.get(giveaway.status, giveaway.status)
    return (
        f"#{giveaway.id} {status} · {escape(short(giveaway.title, 40))} · "
        f"{counted(giveaway.participants, 'участник', 'участника', 'участников')}"
    )


def join_confirmation(giveaway: Giveaway, participant: Participant) -> str:
    lines = [
        "✅ <b>Вы участвуете</b>",
        "",
        f"Розыгрыш: {escape(giveaway.title)}",
        f"Ваш номер: {participant.number}",
    ]
    if giveaway.ends_at:
        lines.append(f"Итоги: {fmt_deadline(giveaway.ends_at)}")
    lines.append("")
    lines.append("Если выиграете — напишу сюда же. Удалять чат не нужно.")
    return "\n".join(lines)


def subscription_gate(giveaway: Giveaway, missing: Sequence[str]) -> str:
    return (
        f"🎁 <b>{escape(giveaway.title)}</b>\n\n"
        "Чтобы участвовать, подпишитесь: "
        + ", ".join(escape(item) for item in missing)
        + "\n\nПотом нажмите «Я подписался»."
    )


def announcement(giveaway: Giveaway, winners: Sequence[Winner], settings: Settings) -> str:
    """Объявление итогов — то, что публикуется в канале."""
    lines = [
        f"🏁 <b>Итоги розыгрыша «{escape(giveaway.title)}»</b>",
        "",
    ]
    if winners:
        lines.append("Победители:")
        lines.extend(f"{item.place}. {mention(item)}" for item in winners)
    else:
        lines.append("Участников не было — разыгрывать оказалось не с кем.")

    lines.extend(
        [
            "",
            f"Участников: {giveaway.participants}",
            f"Зерно жребия: <code>{escape(giveaway.seed)}</code>",
            "",
            "Жребий можно перепроверить: победители — те, у кого меньше",
            "<code>sha256(зерно:ваш id)</code>. Как считать — в описании бота.",
        ]
    )
    return "\n".join(lines)


def mention(winner: Winner) -> str:
    """Как назвать победителя в объявлении: @username или имя со ссылкой."""
    if winner.username:
        return f"@{escape(winner.username)}"
    name = escape(winner.name or "участник")
    return f'<a href="tg://user?id={winner.tg_id}">{name}</a>'


def winner_message(giveaway: Giveaway, winner: Winner, settings: Settings) -> str:
    return (
        f"🎉 <b>Вы выиграли!</b>\n\n"
        f"Розыгрыш: {escape(giveaway.title)}\n"
        f"Приз: {escape(giveaway.prize or giveaway.title)}\n"
        f"Место: {winner.place}\n\n"
        f"Организатор свяжется с вами. Ответьте на это сообщение в течение "
        f"{settings.claim_hours} ч, чтобы приз не разыграли заново."
    )


def proof(giveaway: Giveaway, participants: Sequence[int], winners: Sequence[Winner]) -> str:
    """Расчёт жребия по шагам — для того, кто хочет убедиться сам."""
    lines = [
        f"<b>Проверка розыгрыша #{giveaway.id}</b>",
        "",
        f"Зерно: <code>{escape(giveaway.seed)}</code>",
        f"Участников: {len(participants)}",
        "",
        "Первые билеты по возрастанию:",
    ]
    ordered = sorted(participants, key=lambda item: ticket(giveaway.seed, item))
    for index, tg_id in enumerate(ordered[: max(3, len(winners))], 1):
        mark = " ← победитель" if any(w.tg_id == tg_id for w in winners) else ""
        lines.append(f"{index}. {tg_id}: <code>{ticket(giveaway.seed, tg_id)[:16]}…</code>{mark}")
    return "\n".join(lines)


def post_for_channel(giveaway: Giveaway, settings: Settings) -> str:
    """Готовый текст поста для канала — организатору остаётся скопировать."""
    lines = [f"🎁 <b>{escape(giveaway.title)}</b>"]
    if giveaway.prize:
        lines.append(escape(giveaway.prize))
    lines.extend(
        [
            "",
            f"Победителей: {giveaway.winners_count}",
            f"Итоги: {fmt_deadline(giveaway.ends_at)}",
        ]
    )
    if giveaway.channels:
        lines.append("Условие: подписка на " + ", ".join(escape(c) for c in giveaway.channels))
    lines.extend(["", f"Участвовать: {settings.start_link(giveaway.id)}"])
    return "\n".join(lines)
