"""Демо-данные: услуги барбершопа и расписание, чтобы бота было чем пощупать.

Заполняется только пустая база — команда безопасна для повторного запуска и
ничего не затирает в рабочей.
"""

from __future__ import annotations

from .storage import Storage

DEMO_SERVICES = [
    ("Мужская стрижка", 60, 1500, "Мытьё, стрижка, укладка"),
    ("Стрижка машинкой", 30, 900, ""),
    ("Оформление бороды", 45, 1200, "Моделирование и уход"),
    ("Стрижка + борода", 90, 2400, "Комплекс со скидкой"),
    ("Детская стрижка", 40, 1000, "До 12 лет"),
]

DEMO_MASTERS = ["Аня", "Игорь"]

#: Будни с перерывом на обед, суббота короче, воскресенье выходной.
DEMO_HOURS = [
    ([0, 1, 2, 3, 4], [(10 * 60, 14 * 60), (15 * 60, 20 * 60)]),
    ([5], [(11 * 60, 17 * 60)]),
]


async def seed(storage: Storage) -> bool:
    """Наполнить пустую базу. `False` — данные уже были, ничего не трогали."""
    if await storage.list_services(only_active=False):
        return False

    for title, duration, price, description in DEMO_SERVICES:
        await storage.add_service(title, duration, price, description)
    for name in DEMO_MASTERS:
        await storage.add_master(name)
    for weekdays, windows in DEMO_HOURS:
        await storage.set_windows(weekdays, windows)
    return True
