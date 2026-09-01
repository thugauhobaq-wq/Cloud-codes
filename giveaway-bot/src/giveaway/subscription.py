"""Проверка подписки на каналы — условие участия в розыгрыше."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

log = logging.getLogger(__name__)

#: Статусы участника, при которых считаем человека подписанным.
SUBSCRIBED = {"creator", "administrator", "member", "restricted"}


async def is_subscribed(bot: Bot, channel: str, user_id: int) -> bool:
    """Подписан ли на один канал.

    При ошибке отвечаем «да». Проверка ломается по причинам, в которых
    участник не виноват: бота забыли сделать администратором канала, канал
    переименовали, Telegram ответил ошибкой. Не пустить человека в розыгрыш
    из-за чужой ошибки настройки — хуже, чем пропустить неподписанного.
    """
    if not channel:
        return True
    try:
        member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
    except TelegramAPIError as exc:
        log.warning("не смог проверить подписку на %s: %s", channel, exc)
        return True
    return member.status in SUBSCRIBED


async def missing_channels(bot: Bot, channels: list[str], user_id: int) -> list[str]:
    """Каналы, на которые человек ещё не подписан."""
    missing: list[str] = []
    for channel in channels:
        if not await is_subscribed(bot, channel, user_id):
            missing.append(channel)
    return missing


def channel_link(channel: str) -> str:
    """Ссылка на канал для кнопки. Для приватных (числовой id) её нет."""
    if channel.startswith("@"):
        return f"https://t.me/{channel[1:]}"
    return ""
