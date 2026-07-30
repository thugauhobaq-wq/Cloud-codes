"""Фильтры доступа."""

from __future__ import annotations

from collections.abc import Iterable

from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message, TelegramObject


class IsAdmin(BaseFilter):
    """Пропускать только администраторов.

    Вешается на весь админский роутер:

        router.message.filter(IsAdmin(settings.admins()))
        router.callback_query.filter(IsAdmin(settings.admins()))

    Бота могут найти по имени, а заказы, база клиентов и рассылка — не то,
    что стоит показывать посторонним.
    """

    def __init__(self, admins: Iterable[int]) -> None:
        self.admins = set(admins)

    async def __call__(self, event: TelegramObject) -> bool:
        user = getattr(event, "from_user", None)
        return user is not None and user.id in self.admins


class IsPrivate(BaseFilter):
    """Только личные сообщения.

    Бота добавляют в группы, и там его команды обычно не нужны — а корзина
    или запись, общая на всю группу, ведёт себя странно.
    """

    async def __call__(self, event: TelegramObject) -> bool:
        chat = None
        if isinstance(event, Message):
            chat = event.chat
        elif isinstance(event, CallbackQuery) and isinstance(event.message, Message):
            chat = event.message.chat
        return chat is not None and chat.type == "private"
