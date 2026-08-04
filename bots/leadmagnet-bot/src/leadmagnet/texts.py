"""Тексты бота: приветствие, ответы на кодовые слова, сводки."""

from __future__ import annotations

from collections.abc import Sequence
from html import escape as _html_escape

from .config import Settings
from .models import KIND_TITLES, Broadcast, Lead, Magnet


def escape(text: str) -> str:
    return _html_escape(text or "", quote=False)


def welcome(settings: Settings, magnets: Sequence[Magnet]) -> str:
    """Приветствие: своё из настроек или собранное из списка магнитов."""
    if settings.welcome_text:
        return settings.welcome_text

    lines = [f"<b>{escape(settings.project_name)}</b>", ""]
    ready = [item for item in magnets if item.ready]
    if not ready:
        lines.append("Материалы скоро появятся — загляните позже.")
        return "\n".join(lines)

    lines.append("Напишите кодовое слово, и я пришлю материал:")
    lines.append("")
    lines.extend(f"• <code>{escape(item.code)}</code> — {escape(item.title)}" for item in ready)
    return "\n".join(lines)


def unknown_code(magnets: Sequence[Magnet]) -> str:
    ready = [item for item in magnets if item.ready]
    if not ready:
        return "Пока не знаю такого слова."
    codes = ", ".join(f"<code>{escape(item.code)}</code>" for item in ready)
    return f"Не знаю такого слова. Доступные: {codes}"


def subscription_gate(settings: Settings, magnet: Magnet) -> str:
    return (
        f"Материал «{escape(magnet.title)}» готов к отправке.\n\n"
        "Он для подписчиков канала — подпишитесь и нажмите «Я подписался»."
    )


def magnet_line(magnet: Magnet, settings: Settings) -> str:
    state = "✅" if magnet.active else "🚫"
    if not magnet.ready:
        state = "⚠️"
    line = (
        f"{state} <code>{escape(magnet.code)}</code> — {escape(magnet.title)}"
        f" · {KIND_TITLES.get(magnet.kind, magnet.kind)}"
    )
    if not magnet.ready:
        line += "\n    нечего выдавать: пришлите файл или текст"
    if magnet.followup:
        line += "\n    есть догоняющее сообщение"
    return line


def magnets_list(magnets: Sequence[Magnet], settings: Settings) -> str:
    if not magnets:
        return (
            "Магнитов пока нет.\n\n"
            "Добавить: /magnet_add чеклист | Чек-лист по запуску | текст материала"
        )
    lines = ["<b>Лид-магниты</b>", ""]
    lines.extend(magnet_line(item, settings) for item in magnets)
    return "\n".join(lines)


def magnet_links(magnet: Magnet, settings: Settings, sources: Sequence[str]) -> str:
    lines = [
        f"<b>{escape(magnet.title)}</b>",
        f"Кодовое слово: <code>{escape(magnet.code)}</code>",
        "",
        "Ссылки для постов — по ним материал приходит сразу:",
        f"<code>{escape(settings.start_link(magnet.code))}</code>",
    ]
    for source in sources:
        lines.append(
            f"{escape(source)}: <code>{escape(settings.start_link(magnet.code, source))}</code>"
        )
    lines.append("")
    lines.append("Метка источника видна в /stats — так понятно, какая площадка сработала.")
    return "\n".join(lines)


def stats_text(
    stats: dict[str, int],
    by_magnet: Sequence[tuple[str, str, int]],
    by_source: Sequence[tuple[str, int]],
    daily: Sequence[tuple[str, int]],
    days: int,
) -> str:
    lines = [
        f"<b>Воронка за {days} дн.</b>",
        "",
        f"Подписчиков всего: {stats['total_leads']}",
        f"Пришло за период: {stats['new_leads']}",
        f"Выдач материалов: {stats['deliveries']}",
        f"Заблокировали бота: {stats['blocked']}",
    ]

    if by_magnet:
        lines.extend(["", "<b>По материалам</b>"])
        lines.extend(
            f"• {escape(title)} ({escape(code)}) — {count}" for code, title, count in by_magnet
        )

    if by_source:
        lines.extend(["", "<b>Источники</b>"])
        lines.extend(f"• {escape(label)} — {count}" for label, count in by_source)

    if daily:
        lines.extend(["", "<b>По дням</b>"])
        for day, count in daily[-7:]:
            bar = "▪" * min(count, 20)
            lines.append(f"{day[5:]} {bar} {count}")

    return "\n".join(lines)


def broadcast_report(broadcast: Broadcast) -> str:
    return (
        f"<b>Рассылка #{broadcast.id} завершена</b>\n\n"
        f"Получателей: {broadcast.total}\n"
        f"Доставлено: {broadcast.sent}\n"
        f"Не доставлено: {broadcast.failed}"
    )


def lead_line(lead: Lead) -> str:
    name = escape(lead.name or "без имени")
    username = f" @{escape(lead.username)}" if lead.username else ""
    source = f" · {escape(lead.source)}" if lead.source else ""
    return f"{name}{username}{source}"
