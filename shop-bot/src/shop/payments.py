"""Логика приёма оплаты через Telegram Payments.

Без сети и без базы: сборка счёта и проверка перед списанием — то место, где
ошибка стоит денег, поэтому оно вынесено в чистые функции и закрыто тестами.

Как это работает у Telegram: бот отправляет счёт (`sendInvoice`), покупатель
платит, Telegram присылает `pre_checkout_query` — последний шанс отказаться, и
ответить на него нужно за 10 секунд. Только после подтверждения приходит
`successful_payment` с деньгами.
"""

from __future__ import annotations

from .config import Settings
from .models import PAY_ONLINE, Order

#: Суммы в Telegram Payments — в минимальных единицах валюты (копейках).
#: Рубли из базы нужно умножать, иначе продадите товар в сто раз дешевле.
MINOR_UNITS = 100

PAYLOAD_PREFIX = "order"

#: Ограничения Telegram на поля счёта.
TITLE_LIMIT = 32
DESCRIPTION_LIMIT = 255


def to_minor(amount: int) -> int:
    return int(amount) * MINOR_UNITS


def from_minor(amount: int) -> int:
    return int(amount) // MINOR_UNITS


def order_payload(order_id: int) -> str:
    """`invoice_payload`, по которому платёж связывается с заказом."""
    return f"{PAYLOAD_PREFIX}:{order_id}"


def parse_payload(payload: str | None) -> int | None:
    """Достать номер заказа из payload. `None` — payload не наш."""
    prefix, _, raw = (payload or "").partition(":")
    if prefix != PAYLOAD_PREFIX or not raw.isdigit():
        return None
    return int(raw)


def invoice_title(order: Order, settings: Settings) -> str:
    return f"Заказ №{order.id}"[:TITLE_LIMIT] or f"Заказ №{order.id}"


def invoice_description(order: Order, settings: Settings) -> str:
    """Состав заказа одной строкой — покупатель видит её в окне оплаты."""
    goods = ", ".join(f"{line.title} ×{line.qty}" for line in order.lines)
    text = goods or settings.shop_name
    if len(text) > DESCRIPTION_LIMIT:
        text = text[: DESCRIPTION_LIMIT - 1].rstrip(" ,") + "…"
    return text


def invoice_lines(order: Order) -> list[tuple[str, int]]:
    """Позиции счёта: (подпись, сумма в копейках).

    Доставка идёт отдельной строкой, если она платная: покупатель должен
    видеть, из чего сложилась сумма, а не разбираться, почему на 300 больше.
    """
    lines = [
        (f"{line.title} ×{line.qty}" if line.qty > 1 else line.title, to_minor(line.total))
        for line in order.lines
    ]
    if order.delivery_price:
        lines.append(("Доставка", to_minor(order.delivery_price)))
    return lines


def invoice_total(order: Order) -> int:
    """Сумма счёта в копейках — ровно сумма его позиций."""
    return sum(amount for _, amount in invoice_lines(order))


def precheckout_problem(order: Order | None, total_minor: int, settings: Settings) -> str | None:
    """Причина отказать в списании; `None` — можно платить.

    Между отправкой счёта и оплатой проходят минуты: заказ успевают отменить,
    оплатить с другого устройства или изменить. Списать деньги за такой заказ —
    хуже, чем не принять платёж.
    """
    if order is None:
        return "Заказ не найден. Оформите его заново."
    if order.is_paid:
        return f"Заказ №{order.id} уже оплачен."
    if not order.is_open:
        return f"Заказ №{order.id} отменён, оплата не нужна."
    if order.payment_method != PAY_ONLINE:
        return f"Заказ №{order.id} оформлен с оплатой при получении."
    if total_minor != invoice_total(order):
        # Сумма разошлась — счёт устарел. Молча списать не то, о чём
        # договорились, нельзя.
        return "Сумма заказа изменилась. Оформите заказ заново."
    return None
