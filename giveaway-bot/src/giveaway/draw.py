"""Жеребьёвка, которую может перепроверить любой участник.

Обычный `random.choice` в розыгрыше — слабое место не техническое, а
репутационное: организатор не может доказать, что не выбрал победителя руками,
а недовольный участник не может проверить. Поэтому выбор здесь
детерминированный.

Как это работает:

1. При завершении фиксируется зерно — строка из номера розыгрыша, момента
   завершения и состава участников. Оно публикуется вместе с результатом.
2. Каждому участнику считается `sha256(зерно:tg_id)`.
3. Победители — те, у кого хеш меньше остальных.

Любой, у кого есть зерно и список участников, повторяет расчёт и получает тех
же победителей. Организатор не может «перекрутить» жребий, не изменив состав
участников или момент завершения — а они видны в объявлении.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime

#: Из скольких первых символов хеша делается зерно. 16 шестнадцатеричных
#: символов — 64 бита: подобрать столкновение нереально, а в сообщение влезает.
SEED_LENGTH = 16


def make_seed(giveaway_id: int, finished_at: datetime, participants: Sequence[int]) -> str:
    """Зерно жребия: номер розыгрыша, время завершения и состав участников.

    Состав входит в зерно намеренно: если организатор добавит или уберёт
    участника после объявления, зерно перестанет сходиться, и подмена станет
    видна всем, у кого есть старое объявление.
    """
    payload = "|".join(
        [
            str(giveaway_id),
            finished_at.isoformat(),
            ",".join(str(item) for item in sorted(participants)),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:SEED_LENGTH]


def ticket(seed: str, participant_id: int) -> str:
    """Билет участника: то, что сравнивается при розыгрыше."""
    return hashlib.sha256(f"{seed}:{participant_id}".encode()).hexdigest()


def pick_winners(participants: Sequence[int], count: int, seed: str) -> list[int]:
    """Победители по возрастанию билета. Порядок — это места: первый, второй…

    Участников меньше, чем призов, — вернём всех: розыгрыш на трёх человек с
    пятью призами не должен падать, это дело организатора.
    """
    if count <= 0 or not participants:
        return []

    # Сортируем по билету, а при равенстве (практически невозможном) — по id,
    # чтобы результат не зависел от порядка строк в базе.
    ordered = sorted(set(participants), key=lambda item: (ticket(seed, item), item))
    return ordered[:count]


def verify(participants: Sequence[int], count: int, seed: str, winners: Sequence[int]) -> bool:
    """Совпадает ли объявленный результат с пересчитанным."""
    return pick_winners(participants, count, seed) == list(winners)


def reroll_seed(seed: str, round_number: int) -> str:
    """Зерно для перевыбора, когда победитель не откликнулся.

    Не случайное: получается из прежнего зерна и номера попытки, поэтому
    проверяется так же, как основной розыгрыш, и организатор не может
    крутить его, пока не выпадет нужный человек.
    """
    payload = f"{seed}#{round_number}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:SEED_LENGTH]
