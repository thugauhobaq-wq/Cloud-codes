"""Демо-каталог: маленькая кофейня-магазин, чтобы бота было чем пощупать.

Наполняется только пустая база — команда безопасна для повторного запуска.
"""

from __future__ import annotations

from .storage import Storage

DEMO_CATALOG = {
    "Кофе в зёрнах": [
        ("Эфиопия Иргачеффе, 250 г", 890, "Цитрус, жасмин, светлая обжарка", 12),
        ("Бразилия Сантос, 250 г", 690, "Орех, шоколад, средняя обжарка", 20),
        ("Колумбия Супремо, 1 кг", 2400, "Классика для эспрессо", 5),
    ],
    "Чай": [
        ("Пуэр шу, 100 г", 750, "Плотный, землистый", 8),
        ("Улун Те Гуань Инь, 50 г", 620, "Цветочный, мягкий", 10),
    ],
    "Посуда": [
        ("Воронка V60, керамика", 1450, "На 1–2 чашки", 4),
        ("Кружка 350 мл", 690, "Керамика, ручная работа", 6),
        ("Френч-пресс 600 мл", 1290, "Стекло и сталь", 3),
    ],
    "Подарочные наборы": [
        ("Набор «Знакомство»", 1990, "Три сорта по 100 г и открытка", 7),
        ("Набор «Всё для V60»", 3200, "Воронка, фильтры, кофе", 2),
    ],
}


async def seed(storage: Storage) -> bool:
    """Наполнить пустую базу. `False` — товары уже есть, ничего не трогали."""
    if await storage.list_products(only_available=False):
        return False

    for category_title, products in DEMO_CATALOG.items():
        category_id = await storage.add_category(category_title)
        for title, price, description, stock in products:
            await storage.add_product(title, price, category_id, description, stock)
    return True
