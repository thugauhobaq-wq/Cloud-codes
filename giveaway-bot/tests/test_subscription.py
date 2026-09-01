"""Проверка подписки на каналы.

Условие «подпишись — участвуй» и есть смысл розыгрыша для заказчика, но
ошибаться оно должно в пользу участника: человек не виноват, что бота забыли
сделать администратором канала.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from aiogram.exceptions import TelegramAPIError

from giveaway.subscription import SUBSCRIBED, channel_link, is_subscribed, missing_channels


@dataclass
class Member:
    status: str


class ChannelsBot:
    """Отвечает на get_chat_member по заранее заданной картине мира."""

    def __init__(self, members: dict[str, dict[int, str]], failing: set[str] | None = None) -> None:
        self.members = members
        self.failing = failing or set()
        self.asked: list[tuple[str, int]] = []

    async def get_chat_member(self, chat_id: str, user_id: int):
        self.asked.append((chat_id, user_id))
        if chat_id in self.failing:
            raise TelegramAPIError(method=None, message="Bad Request: chat not found")
        return Member(self.members.get(chat_id, {}).get(user_id, "left"))


@pytest.mark.parametrize("status", sorted(SUBSCRIBED))
async def test_every_membership_status_counts_as_subscribed(status: str):
    bot = ChannelsBot({"@one": {100: status}})

    assert await is_subscribed(bot, "@one", 100) is True


@pytest.mark.parametrize("status", ["left", "kicked"])
async def test_leaving_the_channel_ends_participation(status: str):
    bot = ChannelsBot({"@one": {100: status}})

    assert await is_subscribed(bot, "@one", 100) is False


async def test_an_api_error_lets_the_person_through():
    """Бот не админ канала — это ошибка настройки, а не вина участника."""
    bot = ChannelsBot({}, failing={"@one"})

    assert await is_subscribed(bot, "@one", 100) is True


async def test_an_empty_channel_is_not_a_condition():
    bot = ChannelsBot({})

    assert await is_subscribed(bot, "", 100) is True
    assert bot.asked == []


async def test_missing_channels_lists_only_the_ones_to_subscribe_to():
    bot = ChannelsBot({"@one": {100: "member"}, "@two": {100: "left"}})

    assert await missing_channels(bot, ["@one", "@two"], 100) == ["@two"]


async def test_no_channels_means_no_conditions():
    bot = ChannelsBot({})

    assert await missing_channels(bot, [], 100) == []
    assert bot.asked == []


def test_a_public_channel_gets_a_button_link():
    assert channel_link("@one") == "https://t.me/one"


def test_a_private_channel_has_no_link():
    """У канала по числовому id ссылки нет — кнопку рисовать нечем."""
    assert channel_link("-1001234567890") == ""
