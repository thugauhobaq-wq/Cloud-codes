"""Демо-источник: генерирует правдоподобный набор отзывов без сети.

Нужен, чтобы дашборд можно было открыть и потрогать сразу, а также чтобы
тесты не зависели от доступности чужих сайтов. Данные детерминированы:
один и тот же seed даёт один и тот же набор.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Any

from ..models import Review
from .base import Source

POSITIVE_TEXTS = [
    "Отличное место, персонал вежливый и внимательный. Заказ собрали быстро, всё как на фото.",
    "Очень довольна! Качество на высоте, цена адекватная. Обязательно вернусь ещё.",
    "Быстрая доставка, курьер приехал раньше срока. Упаковка целая, товар без брака.",
    "Ребята профессионалы: помогли выбрать, ничего не навязывали. Всем рекомендую.",
    "Уютная атмосфера, чисто, вкусный кофе. Отдельное спасибо администратору за заботу.",
    "Заказываю не первый раз — стабильно хорошее качество и приятные цены.",
    "Всё понравилось: и обслуживание, и ассортимент. Работают оперативно, без очередей.",
    "Приятно удивили: подарили бонус к заказу и упаковали как подарок. Спасибо!",
    "Мастер сделал аккуратно и в срок, объяснил все нюансы. Надёжно, буду обращаться.",
    "Цена-качество отличное. Доставили за день, всё соответствует описанию.",
]

NEGATIVE_TEXTS = [
    "Ужасное обслуживание, сотрудник нахамил и отказался разбираться с проблемой.",
    "Ждал заказ две недели вместо трёх дней. На звонки не отвечают, деньги не вернули.",
    "Пришёл брак: коробка помята, товар сломан. Возврат оформляют уже месяц.",
    "Очень грязно в зале, столы не убирают, неприятный запах. Больше не приду.",
    "Цены задрали, качество упало. За такие деньги это просто развод.",
    "Продукт оказался просроченный, продавец сделал вид, что это нормально.",
    "Огромная очередь, персонал медленный и равнодушный. Потратил час впустую.",
    "Товар не соответствует описанию, размер меньше заявленного. Не рекомендую.",
    "Навязывают дополнительные услуги, а по факту ничего не сделали. Некомпетентно.",
    "Доставка опоздала, курьер нагрубил, упаковка вскрыта. Очень разочарован.",
]

NEUTRAL_TEXTS = [
    "В целом нормально, но есть к чему стремиться. Ассортимент средний.",
    "Обычное место, ничего особенного. Цены как везде, персонал спокойный.",
    "Заказ пришёл вовремя, но упаковка простенькая. Пользоваться можно.",
    "Кофе неплохой, зато сидеть негде. Как вариант на вынос — сойдёт.",
    "Работает стабильно, местами есть шероховатости с оформлением заказа.",
    "Купил, пользуюсь. Ожидал большего за эти деньги, но и жаловаться не на что.",
]

PROS = ["быстрая доставка", "приятная цена", "вежливые сотрудники", "качественная сборка",
        "удобный сайт", "красивая упаковка", "хороший выбор"]
CONS = ["долгое ожидание", "высокая цена", "мятая упаковка", "мало информации о товаре",
        "неудобный вход", "нет парковки", "путаница с заказом"]

AUTHORS = ["Анна К.", "Дмитрий", "Ольга Петрова", "Сергей", "Марина", "Иван Т.",
           "Екатерина", "Павел", "Юлия", "Артём", "Наталья В.", "Роман",
           "Светлана", "Алексей", "Ксения", "Владимир"]

SOURCE_MIX = [("yandex", 0.34), ("2gis", 0.26), ("wildberries", 0.24), ("ozon", 0.16)]

REPLY = ("Здравствуйте! Спасибо за обратную связь, мы разберёмся в ситуации "
         "и свяжемся с вами.")


class DemoSource(Source):
    name = "demo"
    title = "Демо-данные"
    query_hint = "любое название компании, например «Кофейня Тёплый угол»"

    def collect(self, query: str, limit: int = 120, **options: Any) -> list[Review]:
        company = self.company_key(query, options.get("company")) or "Демо-компания"
        seed = options.get("seed", 42)
        rng = random.Random(f"{company}|{seed}")
        today = options.get("today") or date.today()
        if isinstance(today, str):
            today = date.fromisoformat(today)
        span_days = int(options.get("span_days", 420))

        reviews: list[Review] = []
        for index in range(max(1, limit)):
            source = _weighted_choice(rng, SOURCE_MIX)
            # Ближе к сегодняшнему дню отзывов больше — так выглядят реальные данные.
            offset = int(span_days * rng.random() ** 1.6)
            created = today - timedelta(days=offset)

            roll = rng.random()
            # Со временем доля негатива слегка растёт — чтобы тренд был виден.
            negative_chance = 0.18 + 0.12 * (1 - offset / span_days)
            if roll < negative_chance:
                text = rng.choice(NEGATIVE_TEXTS)
                rating = rng.choice([1, 1, 2, 2, 3])
            elif roll < negative_chance + 0.16:
                text = rng.choice(NEUTRAL_TEXTS)
                rating = rng.choice([3, 3, 4])
            else:
                text = rng.choice(POSITIVE_TEXTS)
                rating = rng.choice([4, 5, 5, 5])

            marketplace = source in ("wildberries", "ozon")
            reviews.append(
                Review(
                    source=source,
                    company=company,
                    text=text,
                    rating=float(rating),
                    author=rng.choice(AUTHORS),
                    date=created.isoformat(),
                    external_id=f"demo-{index}",
                    url=f"https://example.com/{source}/review/{index}",
                    pros=rng.choice(PROS) if marketplace and rng.random() < 0.6 else "",
                    cons=rng.choice(CONS) if marketplace and rng.random() < 0.45 else "",
                    reply=REPLY if rating <= 2 and rng.random() < 0.5 else "",
                )
            )
        return reviews


def _weighted_choice(rng: random.Random, options: list[tuple[str, float]]) -> str:
    roll = rng.random()
    cumulative = 0.0
    for value, weight in options:
        cumulative += weight
        if roll <= cumulative:
            return value
    return options[-1][0]
