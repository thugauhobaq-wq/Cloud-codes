"""Приём оплаты: счёт, подтверждение перед списанием, поступление денег.

Порядок событий у Telegram Payments такой:

1. бот отправляет счёт (`sendInvoice`) — покупатель видит кнопку «Заплатить»;
2. приходит `pre_checkout_query` — последний шанс отказаться, ответить нужно
   за 10 секунд, иначе платёж отменится сам;
3. только после подтверждения приходит `successful_payment` — деньги списаны.

Отказать на шаге 2 нормально. Пропустить платёж за отменённый или уже
оплаченный заказ — нет: возврат придётся делать руками в кабинете провайдера.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

from ..config import Settings
from ..keyboards import CB_PAY, CB_PAY_METHOD, payload
from ..models import PAY_CASH, PAY_ONLINE, PAY_TITLES
from ..notify import Notifier
from ..payments import (
    invoice_description,
    invoice_lines,
    invoice_title,
    order_payload,
    parse_payload,
    precheckout_problem,
)
from ..storage import Storage
from ..texts import escape, money, order_text

log = logging.getLogger(__name__)


def build_payment_router(storage: Storage, notifier: Notifier, settings: Settings) -> Router:
    router = Router(name="payment")

    # ── счёт ──────────────────────────────────────────────────────────────

    @router.callback_query(F.data.startswith(f"{CB_PAY}:"))
    async def send_invoice(callback: CallbackQuery) -> None:
        raw = payload(callback.data)
        order = await storage.get_order(int(raw)) if raw.isdigit() else None

        if order is None or order.customer_id != callback.from_user.id:
            await callback.answer("Заказ не найден")
            return
        if order.is_paid:
            await callback.answer("Этот заказ уже оплачен", show_alert=True)
            return
        if not order.is_open:
            await callback.answer("Заказ отменён — оплата не нужна", show_alert=True)
            return
        if not settings.online_payment_enabled:
            await callback.answer("Оплата картой сейчас недоступна", show_alert=True)
            return

        # Способ фиксируем до отправки счёта: проверка перед списанием
        # сверяется именно с ним.
        await storage.set_payment_method(order.id, PAY_ONLINE)
        order = await storage.get_order(order.id) or order

        prices = [
            LabeledPrice(label=label, amount=amount) for label, amount in invoice_lines(order)
        ]
        try:
            await callback.bot.send_invoice(
                chat_id=callback.from_user.id,
                title=invoice_title(order, settings),
                description=invoice_description(order, settings),
                payload=order_payload(order.id),
                provider_token=settings.payment_provider_token,
                currency=settings.payment_currency,
                prices=prices,
            )
        except TelegramAPIError as exc:
            # Чаще всего это неверный токен провайдера или валюта, которую он
            # не поддерживает. Покупатель в этом не виноват.
            log.error("не смог выставить счёт по заказу %s: %s", order.id, exc)
            await callback.answer("Не получилось выставить счёт, напишем вам сами", show_alert=True)
            await notifier.send_admins(
                f"⚠️ Не удалось выставить счёт по заказу №{order.id}: {escape(str(exc))}\n"
                "Проверьте PAYMENT_PROVIDER_TOKEN и PAYMENT_CURRENCY."
            )
            return

        await callback.answer()

    @router.callback_query(F.data.startswith(f"{CB_PAY_METHOD}:"))
    async def choose_method(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":")
        if len(parts) < 3 or not parts[1].isdigit():
            await callback.answer()
            return

        order_id, method = int(parts[1]), parts[2]
        order = await storage.get_order(order_id)
        if order is None or order.customer_id != callback.from_user.id:
            await callback.answer("Заказ не найден")
            return
        if order.is_paid:
            await callback.answer("Заказ уже оплачен", show_alert=True)
            return
        if method == PAY_CASH and not settings.cash_payment_enabled:
            await callback.answer("Оплата при получении недоступна", show_alert=True)
            return

        updated = await storage.set_payment_method(order_id, method) or order
        await callback.answer("Записал")
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_text(
                    f"✅ <b>Заказ №{updated.id} принят</b>\n\n"
                    + order_text(updated, settings)
                    + "\n\nПродавец свяжется с вами для подтверждения."
                )
            except TelegramAPIError:
                log.debug("не смог обновить сообщение о заказе", exc_info=True)
        await notifier.payment_method_chosen(updated)

    # ── подтверждение перед списанием ─────────────────────────────────────

    @router.pre_checkout_query()
    async def confirm_before_charge(query: PreCheckoutQuery) -> None:
        order_id = parse_payload(query.invoice_payload)
        order = await storage.get_order(order_id) if order_id else None
        problem = precheckout_problem(order, query.total_amount, settings)

        if problem:
            log.info("отказ в оплате по заказу %s: %s", order_id, problem)
            await query.answer(ok=False, error_message=problem)
            return

        await query.answer(ok=True)

    # ── деньги пришли ─────────────────────────────────────────────────────

    @router.message(F.successful_payment)
    async def payment_received(message: Message) -> None:
        payment = message.successful_payment
        order_id = parse_payload(payment.invoice_payload)
        if order_id is None:
            # Платёж не от этого бота или payload испорчен: деньги пришли, а
            # заказа нет — разбираться должен человек.
            log.error("платёж с неизвестным payload: %s", payment.invoice_payload)
            await notifier.send_admins(
                "⚠️ Пришла оплата, которую не удалось связать с заказом.\n"
                f"payload: <code>{escape(str(payment.invoice_payload))}</code>\n"
                f"charge_id: <code>{escape(payment.telegram_payment_charge_id)}</code>"
            )
            return

        order = await storage.mark_paid(order_id, payment.telegram_payment_charge_id)
        if order is None:
            # Повторный апдейт после сбоя сети: заказ уже оплачен, второе
            # уведомление продавцу и вторая выручка в статистике не нужны.
            log.info("повторный successful_payment по заказу %s — пропускаю", order_id)
            return

        await message.answer(
            f"✅ <b>Оплата получена</b>\n\nЗаказ №{order.id} на "
            f"{money(order.total, settings)} оплачен. Собираем и свяжемся с вами."
        )
        await notifier.payment_received(order)

    return router


def payment_method_title(method: str) -> str:
    return PAY_TITLES.get(method, method)
