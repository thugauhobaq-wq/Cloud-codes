"""Работа с сообщениями и callback_data.

Здесь собрано то, обо что спотыкается каждый бот: сообщение под кнопкой может
быть старше 48 часов, редактирование одинакового текста считается ошибкой,
а длинный ответ не влезает в лимит Telegram.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

log = logging.getLogger(__name__)

#: Предел одного сообщения в Telegram.
TEXT_LIMIT = 4096


def payload(data: str | None, index: int = 1) -> str:
    """Часть callback_data после префикса: `svc:12` → `12`."""
    chunks = (data or "").split(":")
    return chunks[index] if len(chunks) > index else ""


def parts(data: str | None) -> list[str]:
    return (data or "").split(":")


def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Разбить длинный текст по строкам, не разрывая разметку посреди строки.

    Длинные списки — заказы за месяц, база подписчиков — обрезаются Telegram
    молча, и владелец видит половину отчёта, не зная об этом.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        # Строка длиннее лимита сама по себе — режем как есть, вариантов нет.
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    if current:
        chunks.append(current)
    return chunks


async def send(bot: Bot, chat_id: int, text: str, **kwargs) -> bool:
    """Отправить, разбивая длинный текст. `False` — получатель недоступен."""
    pieces = split_text(text)
    for index, piece in enumerate(pieces):
        try:
            # Клавиатуру вешаем только на последний кусок: иначе кнопки
            # оказываются в середине простыни.
            await bot.send_message(
                chat_id, piece, **(kwargs if index == len(pieces) - 1 else _without_markup(kwargs))
            )
        except TelegramAPIError as exc:
            log.info("не доставлено %s: %s", chat_id, exc)
            return False
    return True


def _without_markup(kwargs: dict) -> dict:
    return {key: value for key, value in kwargs.items() if key != "reply_markup"}


async def edit(callback: CallbackQuery, text: str, reply_markup=None) -> bool:
    """Заменить текст сообщения под кнопкой.

    Редактирование вместо новых сообщений: иначе после четырёх шагов выбора
    чат превращается в простыню из устаревших клавиатур.
    """
    if not isinstance(callback.message, Message):
        return False
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
        return True
    except TelegramAPIError:
        # Текст не изменился (двойное нажатие) или сообщение слишком старое.
        log.debug("не смог отредактировать сообщение", exc_info=True)
        return False


async def replace(callback: CallbackQuery, text: str, reply_markup=None) -> None:
    """Показать новый экран: отредактировать текст, а карточку с фото — заменить.

    Текстовое сообщение нельзя превратить в фото и наоборот, поэтому карточку
    с картинкой убираем и шлём новое сообщение.
    """
    message = callback.message
    editable = isinstance(message, Message) and not message.photo
    if editable and await edit(callback, text, reply_markup):
        return
    await drop(callback)
    await send_to_user(callback, text, reply_markup=reply_markup)


async def drop(callback: CallbackQuery) -> None:
    """Убрать сообщение, под которым нажали кнопку."""
    if not isinstance(callback.message, Message):
        return
    try:
        await callback.message.delete()
    except TelegramAPIError:
        # Старше 48 часов Telegram удалять не даёт — не беда.
        log.debug("не удалил сообщение", exc_info=True)


async def send_to_user(callback: CallbackQuery, text: str, **kwargs) -> bool:
    """Написать нажавшему кнопку напрямую.

    Через бота, а не через `callback.message.answer`: сообщение с кнопкой
    может оказаться недоступным (старше 48 часов), и ответить на него нельзя.
    """
    if callback.bot is None:
        return False
    return await send(callback.bot, callback.from_user.id, text, **kwargs)
