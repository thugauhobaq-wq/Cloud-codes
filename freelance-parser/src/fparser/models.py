"""Единая модель заказа, к которой приводятся все площадки."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class Order:
    """Заказ с любой из бирж.

    Площадки отдают очень разный набор полей: RSS-ленты часто не содержат ни бюджета,
    ни числа откликов, поэтому всё, кроме идентификатора, заголовка и ссылки,
    необязательно. Фильтры и форматтер обязаны переживать `None`.
    """

    source: str
    """Слаг площадки: kwork / flru / habr / weblancer."""

    native_id: str
    """Идентификатор заказа внутри площадки."""

    title: str
    url: str

    description: str = ""
    budget_min: int | None = None
    budget_max: int | None = None
    currency: str = "₽"
    category: str | None = None
    published_at: datetime | None = None
    responses_count: int | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def uid(self) -> str:
        """Глобальный ключ дедупликации.

        Идентификаторы на разных биржах пересекаются (везде встречается заказ №1),
        поэтому ключ обязательно включает площадку.
        """
        return f"{self.source}:{self.native_id}"

    @property
    def budget_value(self) -> int | None:
        """Сумма для сравнения с порогом «минимальный бюджет».

        Предпочитаем нижнюю границу: если заказчик написал «5000–20000», решать,
        интересен ли заказ, надо по гарантированным 5000, а не по мечте о 20000.
        Когда известен только потолок («до 120 000»), берём его — иначе заказ
        останется вовсе без оценки и порог по нему не сработает.
        """
        if self.budget_min is not None:
            return self.budget_min
        return self.budget_max

    @property
    def searchable_text(self) -> str:
        """Текст, по которому работают ключевые и стоп-слова."""
        return f"{self.title}\n{self.description}"
