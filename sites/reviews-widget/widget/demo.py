"""Демо-отзывы: чтобы виджет можно было показать клиенту до первой продажи."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from .models import Review

NAMES = [
    "Анна", "Дмитрий", "Ольга К.", "Сергей", "Марина", "Илья", "Екатерина",
    "Павел Н.", "Юлия", "Артём", "Наталья", "Роман", "Светлана", "Кирилл",
    "Виктория", "Максим", "Алина", "Георгий", "Полина", "Тимур",
]

GOOD = [
    "Заказывала второй раз, всё привезли в срок и аккуратно упаковали. "
    "Отдельное спасибо курьеру — предупредил за полчаса.",
    "Ребята сделали всё за один день. Цена совпала с той, что назвали по телефону, "
    "без сюрпризов в конце.",
    "Приятно, что отвечают в мессенджере быстро и по делу. Вопрос решили за пятнадцать минут.",
    "Качество на высоте, придирок нет. Пользуюсь третий месяц — всё как в первый день.",
    "Хорошее место, вернусь ещё. Персонал вежливый, всё объяснили новичку спокойно и подробно.",
    "Спасибо за помощь с подбором! Сначала сомневалась, но подсказали вариант дешевле — и он подошёл.",
    "Работают чётко: приехали вовремя, убрали за собой, отчитались по каждому пункту.",
    "Долго выбирал, в итоге доволен. Сервис на уровне, а поддержка реально помогает, а не отписывается.",
]

OKAY = [
    "В целом нормально. Немного задержали доставку, но предупредили заранее и сделали скидку.",
    "Сам продукт хороший, а вот сайтом пользоваться неудобно — оформлял заказ через оператора.",
    "Свои деньги отрабатывают. Есть мелкие шероховатости, но ничего критичного.",
    "Ожидал большего от упаковки, зато содержимое полностью соответствует описанию.",
]

BAD = [
    "Ждал неделю вместо обещанных двух дней. Деньги вернули, но осадок остался.",
    "Менеджер путался в характеристиках, пришлось перепроверять всё самому.",
]

REPLIES = {
    "Ждал неделю": "Извините за задержку — поставка застряла на складе. Компенсацию отправили "
                   "на карту, в следующий раз доставим бесплатно.",
    "Менеджер путался": "Спасибо, что написали. Провели обучение с менеджером, "
                        "по вашему вопросу перезвонили и всё уточнили.",
}


def generate(site_key: str, count: int = 18, seed: int | None = None) -> list[Review]:
    """Отзывы за последние полгода с правдоподобным перекосом в позитив."""
    rng = random.Random(seed)
    now = datetime.now(timezone.utc)
    reviews: list[Review] = []

    for index in range(count):
        roll = rng.random()
        if roll < 0.72:
            text, rating = rng.choice(GOOD), rng.choice([5, 5, 5, 4])
        elif roll < 0.92:
            text, rating = rng.choice(OKAY), rng.choice([3, 4])
        else:
            text, rating = rng.choice(BAD), rng.choice([1, 2])

        created = now - timedelta(days=rng.randint(0, 180), hours=rng.randint(0, 23))
        review = Review.build(
            site=site_key,
            text=text,
            rating=rating,
            author=rng.choice(NAMES),
            source="demo",
        )
        review.created_at = created.replace(microsecond=0).isoformat()
        review.published_at = review.created_at
        review.status = "published"
        for marker, reply in REPLIES.items():
            if text.startswith(marker):
                review.reply = reply
        # Один и тот же текст от разных людей — оставляем только уникальные.
        if any(existing.uid == review.uid for existing in reviews):
            continue
        reviews.append(review)

    reviews.sort(key=lambda item: item.created_at, reverse=True)
    # Закрепляем свежий пятизвёздочный — примерно так это и делают вручную.
    for review in reviews:
        if review.rating == 5:
            review.pinned = True
            break
    return reviews
