"""Обработчики: заказ словами, карточка с кнопками, сборка, история.

Доступ только у админов, и это не перестраховка: бот пушит в GitHub под
токеном владельца, то есть любой, кто до него дотянется, создаёт репозитории
в чужом аккаунте.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, CallbackQuery, Message
from botkit import IsAdmin, IsPrivate, messaging

from . import keyboards, texts
from .config import Settings
from .factory import Factory, FactoryError
from .orders import Order, OrderError, parse
from .storage import Storage

log = logging.getLogger(__name__)


def admin_commands() -> list[BotCommand]:
    return [
        BotCommand(command="help", description="как заказать бота"),
        BotCommand(command="builds", description="последние сборки"),
        BotCommand(command="stats", description="сводка"),
    ]


def build_router(storage: Storage, factory: Factory, settings: Settings) -> Router:
    root = Router(name="root")
    root.include_router(_owner_router(storage, factory, settings))
    # Роутер для посторонних идёт последним: сначала владелец, потом отказ.
    root.include_router(_guest_router(settings))
    return root


def _owner_router(storage: Storage, factory: Factory, settings: Settings) -> Router:
    router = Router(name="owner")
    router.message.filter(IsPrivate(), IsAdmin(settings.admins()))
    router.callback_query.filter(IsAdmin(settings.admins()))

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await message.answer(texts.START)

    @router.message(Command("help"))
    async def help_(message: Message) -> None:
        await message.answer(texts.HELP)

    @router.message(Command("builds"))
    async def builds(message: Message) -> None:
        await message.answer(texts.builds(await storage.recent(10)))

    @router.message(Command("stats"))
    async def stats(message: Message) -> None:
        await message.answer(texts.stats(await storage.stats()))

    @router.message(F.text)
    async def order(message: Message) -> None:
        try:
            wanted = parse(
                message.text or "",
                mode=settings.publish_mode,
                private=settings.private_repos,
            )
        except OrderError as exc:
            await message.answer(f"{exc}\n\n{texts.HELP}")
            return

        build_id = await storage.add(wanted, message.from_user.id if message.from_user else 0)
        await message.answer(
            texts.card(wanted, build_id),
            reply_markup=keyboards.order_card(build_id, wanted),
        )

    @router.callback_query(F.data.startswith(f"{keyboards.PREFIX}:"))
    async def act(callback: CallbackQuery) -> None:
        build_id = int(messaging.payload(callback.data, 1) or 0)
        name = messaging.payload(callback.data, 2)

        build = await storage.get(build_id)
        if build is None:
            await callback.answer("Заказ не найден — напишите новый")
            return

        if name in (keyboards.MODE, keyboards.PRIVACY):
            await _tune(callback, storage, build_id, build.order, name)
        elif name == keyboards.CANCEL:
            await _cancel(callback, storage, build_id)
        elif name == keyboards.RETRY:
            await _retry(callback, storage, build_id, build.order)
        elif name == keyboards.GO:
            await _go(callback, storage, factory, build_id, build.order)
        else:
            await callback.answer()

    return router


def _guest_router(settings: Settings) -> Router:
    """Всё, что не разобрал роутер владельца. В группах бот молчит."""
    router = Router(name="guest")
    router.message.filter(IsPrivate())

    @router.message()
    async def denied(message: Message) -> None:
        user = message.from_user.id if message.from_user else None
        if settings.is_admin(user):
            # Владелец прислал не текст: стикер, фото, голосовое.
            await message.answer("Заказ нужен текстом — напишите словами. /help")
            return
        await message.answer(texts.DENIED)

    @router.callback_query()
    async def denied_callback(callback: CallbackQuery) -> None:
        await callback.answer(texts.DENIED, show_alert=True)

    return router


async def _tune(
    callback: CallbackQuery, storage: Storage, build_id: int, order: Order, name: str
) -> None:
    """Поправить заказ кнопкой — пока сборка не началась."""
    changed = order.toggled_mode() if name == keyboards.MODE else order.toggled_private()
    if not await storage.update_order(build_id, changed):
        await callback.answer("Заказ уже в работе")
        return
    await messaging.edit(
        callback, texts.card(changed, build_id), keyboards.order_card(build_id, changed)
    )
    await callback.answer()


async def _cancel(callback: CallbackQuery, storage: Storage, build_id: int) -> None:
    if not await storage.drop(build_id):
        await callback.answer("Сборка уже началась, отменять поздно")
        return
    await messaging.edit(callback, f"Заказ №{build_id} отменён.")
    await callback.answer()


async def _retry(callback: CallbackQuery, storage: Storage, build_id: int, order: Order) -> None:
    if not await storage.retry(build_id):
        await callback.answer("Этот заказ повторить нельзя")
        return
    await messaging.edit(
        callback, texts.card(order, build_id), keyboards.order_card(build_id, order)
    )
    await callback.answer()


async def _go(
    callback: CallbackQuery, storage: Storage, factory: Factory, build_id: int, order: Order
) -> None:
    """Собрать и опубликовать.

    `start()` меняет статус одним UPDATE с проверкой: второе нажатие кнопки
    не должно запускать вторую сборку и второй пуш в тот же репозиторий.
    """
    if not await storage.start(build_id):
        await callback.answer("Уже собираю")
        return

    await callback.answer("Собираю…")
    await messaging.edit(callback, texts.building(order))

    try:
        result = await factory.build(order)
    except FactoryError as exc:
        await storage.fail(build_id, str(exc))
        await messaging.send_to_user(
            callback, texts.failed(str(exc)), reply_markup=keyboards.retry(build_id)
        )
        return
    except Exception as exc:
        # Заказчику нужен ответ, а не тишина: любая неожиданная ошибка
        # становится видимой неудачей с кнопкой «Повторить».
        log.exception("сборка №%s упала", build_id)
        await storage.fail(build_id, f"внутренняя ошибка: {exc}")
        await messaging.send_to_user(
            callback, texts.failed(str(exc)), reply_markup=keyboards.retry(build_id)
        )
        return

    await storage.finish(build_id, result.url, result.repo)
    build = await storage.get(build_id)
    if build is not None:
        await messaging.send_to_user(
            callback, texts.done(build, result.url, result.published.files)
        )
