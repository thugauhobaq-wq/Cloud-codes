"""Клавиатуры записи: клиент не должен застревать внутри сценария.

Проверяется не расположение кнопок, а свойство: с любого шага есть выход в
меню. Первый шаг раньше был тупиком — уйти с него можно было только заново
написав команду, чего клиент не делает: он бросает бота.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from aiogram.types import InlineKeyboardMarkup

from booking.handlers.admin import admin_commands
from booking.handlers.client import client_commands
from booking.keyboards import (
    CB_HOME,
    confirm_keyboard,
    days_keyboard,
    masters_keyboard,
    services_keyboard,
    slots_keyboard,
)
from booking.models import Master, Service

MSK = ZoneInfo("Europe/Moscow")


def buttons(keyboard: InlineKeyboardMarkup) -> list:
    return [button for row in keyboard.inline_keyboard for button in row]


def has_home(keyboard: InlineKeyboardMarkup) -> bool:
    home = f"{CB_HOME}:"
    return any((button.callback_data or "").startswith(home) for button in buttons(keyboard))


SERVICES = [Service(id=1, title="Стрижка", duration_min=60, price=1500)]
MASTERS = [Master(id=1, name="Аня"), Master(id=2, name="Игорь")]
DAYS = [date(2026, 7, 29), date(2026, 7, 30)]
SLOTS = [datetime(2026, 7, 29, 10, tzinfo=UTC), datetime(2026, 7, 29, 11, tzinfo=UTC)]


@pytest.mark.parametrize(
    ("step", "keyboard"),
    [
        ("услуга", services_keyboard(SERVICES)),
        ("мастер", masters_keyboard(MASTERS)),
        ("день", days_keyboard(DAYS, set(), DAYS[0], with_back=True)),
        ("день без шага мастера", days_keyboard(DAYS, set(), DAYS[0], with_back=False)),
        ("время", slots_keyboard(SLOTS, MSK)),
        ("подтверждение", confirm_keyboard()),
    ],
)
def test_every_step_has_a_way_out(step: str, keyboard: InlineKeyboardMarkup):
    assert has_home(keyboard), f"с шага «{step}» некуда уйти"


def test_the_first_step_offers_the_services_themselves():
    keyboard = services_keyboard(SERVICES)

    titles = [button.text for button in buttons(keyboard)]
    assert any("Стрижка" in title for title in titles)


def test_days_without_free_time_are_visible_but_dead():
    """Убрать занятый день из календаря — значит заставить клиента гадать."""
    keyboard = days_keyboard(DAYS, {DAYS[0]}, DAYS[0], with_back=False)

    busy = [button for button in buttons(keyboard) if "✖️" in button.text]
    assert busy and all((button.callback_data or "").startswith("noop") for button in busy)


def test_going_back_is_offered_where_there_is_somewhere_to_go():
    assert any("⬅️" in button.text for button in buttons(masters_keyboard(MASTERS)))
    assert any("⬅️" in button.text for button in buttons(slots_keyboard(SLOTS, MSK)))


def test_the_calendar_hides_the_back_button_when_there_was_no_master_step():
    keyboard = days_keyboard(DAYS, set(), DAYS[0], with_back=False)

    assert not any("⬅️" in button.text for button in buttons(keyboard))


# ── команды в меню ────────────────────────────────────────────────────────


def test_client_and_admin_menus_do_not_overlap():
    """Клиент видел в меню /dayoff и /stats — команды, которые ему не работают."""
    client = {item.command for item in client_commands()}
    admin = {item.command for item in admin_commands()}

    assert not (client & admin)


def test_client_menu_covers_what_a_client_actually_does():
    commands = {item.command for item in client_commands()}

    assert {"start", "book", "my"} <= commands
