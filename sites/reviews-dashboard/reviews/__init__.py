"""Парсер отзывов о компании с веб-дашбордом.

Пакет собирает отзывы с внешних площадок (Яндекс.Карты, 2ГИС, Wildberries,
Ozon, локальные выгрузки), размечает тональность и отдаёт сводку в веб-интерфейс.
Внешних зависимостей нет — только стандартная библиотека Python.
"""

from .models import Review
from .sentiment import analyze
from .storage import Storage

__all__ = ["Review", "Storage", "analyze"]
__version__ = "1.0.0"
