"""Публикация меню команд: разделение по ролям и устойчивость к обрыву связи.

Связь сервера с Telegram моргает, и одиночный set_my_commands ловит таймаут.
Раньше это молча оставляло клиенту админское меню; проверяем, что повтор
доводит дело до конца, а области видимости разложены верно.
"""

from __future__ import annotations

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import BotCommandScopeChat, BotCommandScopeDefault

from booking.__main__ import publish_commands
from booking.handlers.admin import admin_commands
from booking.handlers.client import client_commands


class FakeBot:
    """Запоминает, что и в какую область выставили. Умеет падать заданное число раз."""

    def __init__(self, fail_times: int = 0) -> None:
        self.calls: list[tuple[object, list]] = []
        self.fail_times = fail_times
        self.attempts = 0

    async def set_my_commands(self, commands, scope=None) -> None:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise TelegramNetworkError(method=None, message="Request timeout error")
        self.calls.append((scope, [c.command for c in commands]))


def scope_of(bot: FakeBot, kind: type) -> list | None:
    for scope, commands in bot.calls:
        if isinstance(scope, kind):
            return commands
    return None


async def test_client_gets_the_default_scope():
    bot = FakeBot()

    await publish_commands(bot, [1085071026])

    default = scope_of(bot, BotCommandScopeDefault)
    assert default == [c.command for c in client_commands()]
    assert "dayoff" not in default and "stats" not in default


async def test_admin_gets_their_own_chat_scope():
    bot = FakeBot()

    await publish_commands(bot, [1085071026])

    chat_scoped = [(s, c) for s, c in bot.calls if isinstance(s, BotCommandScopeChat)]
    assert chat_scoped
    scope, commands = chat_scoped[0]
    assert scope.chat_id == 1085071026
    assert commands == [c.command for c in admin_commands()]


async def test_a_flaky_link_does_not_leave_the_menu_unset():
    """Два таймаута подряд — команды всё равно должны выставиться с третьей попытки."""
    bot = FakeBot(fail_times=2)

    await publish_commands(bot, [])

    assert scope_of(bot, BotCommandScopeDefault) == [c.command for c in client_commands()]


async def test_it_gives_up_after_the_limit_without_crashing(monkeypatch):
    """Если связь так и не поднялась, старт бота не должен падать из-за меню."""
    # Без пауз между попытками, чтобы тест не ждал.
    monkeypatch.setattr("booking.__main__.asyncio.sleep", _no_sleep)
    bot = FakeBot(fail_times=999)

    # Не бросает, просто ничего не выставилось.
    await publish_commands(bot, [1085071026])

    assert bot.calls == []


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.mark.parametrize("admins", [[], [1], [1, 2, 3]])
async def test_every_admin_is_covered(admins: list[int]):
    bot = FakeBot()

    await publish_commands(bot, admins)

    chat_ids = {s.chat_id for s, _ in bot.calls if isinstance(s, BotCommandScopeChat)}
    assert chat_ids == set(admins)
