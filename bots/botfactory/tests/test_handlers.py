"""Проверка сборки бота как целого: роутеры, фильтры, команды.

Полноценный диалог здесь не разыгрывается — Telegram для этого не нужен, но
нужен его протокол. А вот собрать бота из частей стоит: опечатка в фильтре
или в имени обработчика ломает запуск, и узнать об этом лучше в тестах.
"""

from __future__ import annotations

from botkit.publish import MODE_REPO

from botfactory.config import Settings
from botfactory.factory import Factory
from botfactory.handlers import admin_commands, build_router
from botfactory.keyboards import CANCEL, GO, MODE, PRIVACY, action, order_card
from botfactory.orders import Order
from botfactory.storage import Storage

ORDER = Order(package="shop", title="Магазин у дома", mode=MODE_REPO, private=True)


def test_router_is_assembled(settings: Settings, storage: Storage):
    router = build_router(storage, Factory(settings), settings)

    # Роутер для посторонних — последний: сначала владелец, потом отказ.
    assert [child.name for child in router.sub_routers] == ["owner", "guest"]


def test_commands_are_offered_to_the_owner():
    names = [command.command for command in admin_commands()]

    assert names == ["help", "builds", "stats"]


def test_card_buttons_carry_the_order_id():
    markup = order_card(7, ORDER)
    data = [button.callback_data for row in markup.inline_keyboard for button in row]

    assert data == [action(7, GO), action(7, MODE), action(7, PRIVACY), action(7, CANCEL)]
    # Лимит callback_data — 64 байта, и id заказа в него обязан влезать.
    assert all(len(item.encode()) <= 64 for item in data)


def test_buttons_offer_the_opposite_choice(settings: Settings):
    """Кнопка показывает, куда переключит, а не текущее состояние."""
    labels = [
        button.text for row in order_card(7, ORDER).inline_keyboard for button in row
    ]

    assert "→ ветка" in labels
    assert "→ публичный" in labels
