"""Админка продавца: товары, категории, заказы, остатки, статистика, выгрузка.

Управление командами, а не веб-панелью: у маленького продавца нет сайта — в
этом и смысл бота, — а телефон с Telegram есть всегда.
"""

from __future__ import annotations

import csv
import io
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..config import Settings
from ..keyboards import CB_ORDER_STATUS, order_admin_keyboard, payload
from ..models import (
    DELIVERY_TITLES,
    STATUS_ACCEPTED,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_NEW,
    STATUS_TITLES,
)
from ..notify import Notifier
from ..storage import Storage
from ..texts import escape, money, order_short, order_text

log = logging.getLogger(__name__)

ADMIN_HELP = """<b>Админка магазина</b>

<b>Заказы</b>
/orders — новые и принятые
/orders all — последние 20 любых
/order 12 — карточка заказа с кнопками
/export — выгрузка заказов в CSV

<b>Товары</b>
/products — список, нажатие скрывает и возвращает
/product_add Чехол | 990 | Аксессуары | силикон, чёрный | 10
   название | цена | категория | описание | остаток
/price 12 1290 — изменить цену
/stock 12 5 — остаток (- снимает учёт)
/photo 12 — пришлите фото с этой подписью

<b>Категории</b>
/categories
/category_add Аксессуары

<b>Прочее</b>
/lowstock — что заканчивается
/stats — продажи за 30 дней"""


def build_admin_router(storage: Storage, notifier: Notifier, settings: Settings) -> Router:
    router = Router(name="admin")
    admins = settings.admins()

    router.message.filter(F.from_user.id.in_(admins))
    router.callback_query.filter(F.from_user.id.in_(admins))

    # ── заказы ────────────────────────────────────────────────────────────

    @router.message(Command("admin"))
    async def cmd_admin(message: Message) -> None:
        await message.answer(ADMIN_HELP)

    @router.message(Command("orders"))
    async def cmd_orders(message: Message, command: CommandObject) -> None:
        show_all = (command.args or "").strip().lower() in {"all", "все"}
        statuses = None if show_all else [STATUS_NEW, STATUS_ACCEPTED]
        orders = await storage.list_orders(statuses=statuses, limit=20)

        if not orders:
            await message.answer(
                "Активных заказов нет." if not show_all else "Заказов пока не было."
            )
            return

        lines = ["<b>Заказы</b>", ""]
        lines.extend(order_short(item, settings) for item in orders)
        lines.append("")
        lines.append("Открыть: /order 12")
        await message.answer("\n".join(lines))

        # Новые показываем карточками с кнопками — их надо обработать сейчас,
        # а не листать список.
        for item in orders:
            if item.status == STATUS_NEW:
                await message.answer(
                    order_text(item, settings, for_admin=True),
                    reply_markup=order_admin_keyboard(item.id, item.status),
                )

    @router.message(Command("order"))
    async def cmd_order(message: Message, command: CommandObject) -> None:
        raw = (command.args or "").strip().lstrip("#№")
        order = await storage.get_order(int(raw)) if raw.isdigit() else None
        if order is None:
            await message.answer("Не нашёл такой заказ. Формат: /order 12")
            return
        await message.answer(
            order_text(order, settings, for_admin=True),
            reply_markup=order_admin_keyboard(order.id, order.status),
        )

    @router.callback_query(F.data.startswith(f"{CB_ORDER_STATUS}:"))
    async def change_status(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":")
        if len(parts) < 3 or not parts[1].isdigit():
            await callback.answer()
            return

        order_id, status = int(parts[1]), parts[2]
        if status not in STATUS_TITLES:
            await callback.answer("Неизвестный статус")
            return

        order = await storage.set_order_status(order_id, status)
        if order is None:
            await callback.answer("Статус уже такой")
            return

        await callback.answer(STATUS_TITLES[status])
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                order_text(order, settings, for_admin=True),
                reply_markup=order_admin_keyboard(order.id, order.status),
            )
        await notifier.order_status(order)

        if status == STATUS_CANCELLED:
            # Отменённый заказ возвращает товары на склад — стоит сказать об этом,
            # иначе продавец решит, что остатки «слиплись».
            await callback.bot.send_message(
                callback.from_user.id, "Товары из заказа вернулись в наличие."
            )

    @router.message(Command("export"))
    async def cmd_export(message: Message) -> None:
        orders = await storage.list_orders(limit=1000)
        if not orders:
            await message.answer("Экспортировать пока нечего.")
            return

        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";")
        writer.writerow(
            ["Заказ", "Дата", "Статус", "Покупатель", "Телефон", "Способ", "Адрес",
             "Товары", "Сумма товаров", "Доставка", "Итого", "Комментарий"]
        )
        for order in orders:
            goods = "; ".join(f"{line.title} ×{line.qty}" for line in order.lines)
            writer.writerow(
                [
                    order.id,
                    order.created_at.strftime("%d.%m.%Y %H:%M") if order.created_at else "",
                    STATUS_TITLES.get(order.status, order.status),
                    order.name,
                    order.phone,
                    DELIVERY_TITLES.get(order.delivery, order.delivery),
                    order.address,
                    goods,
                    order.goods_total,
                    order.delivery_price,
                    order.total,
                    order.comment,
                ]
            )

        # utf-8-sig — чтобы Excel не показал кириллицу кракозябрами.
        data = buffer.getvalue().encode("utf-8-sig")
        await message.answer_document(
            BufferedInputFile(data, filename="orders.csv"),
            caption=f"Заказов в выгрузке: {len(orders)}",
        )

    # ── товары ────────────────────────────────────────────────────────────

    @router.message(Command("products"))
    async def cmd_products(message: Message) -> None:
        products = await storage.list_products(only_available=False)
        if not products:
            await message.answer(
                "Товаров пока нет.\nДобавить: /product_add Чехол | 990 | Аксессуары | силикон | 10"
            )
            return
        await message.answer(
            "<b>Товары</b>\n\nНажмите, чтобы скрыть или вернуть в продажу.",
            reply_markup=_products_keyboard(products, settings),
        )

    @router.callback_query(F.data.startswith("ptoggle:"))
    async def toggle_product(callback: CallbackQuery) -> None:
        raw = payload(callback.data)
        product = await storage.get_product(int(raw)) if raw.isdigit() else None
        if product is None:
            await callback.answer("Товар не найден")
            return

        await storage.update_product(product.id, active=int(not product.active))
        await callback.answer("Скрыл" if product.active else "Вернул в продажу")
        if isinstance(callback.message, Message):
            products = await storage.list_products(only_available=False)
            await callback.message.edit_reply_markup(
                reply_markup=_products_keyboard(products, settings)
            )

    @router.message(Command("product_add"))
    async def cmd_product_add(message: Message, command: CommandObject) -> None:
        parts = [part.strip() for part in (command.args or "").split("|")]
        if len(parts) < 2 or not parts[0]:
            await message.answer(
                "Формат: /product_add Чехол | 990 | Аксессуары | силикон, чёрный | 10\n"
                "Обязательны название и цена; категория, описание и остаток — по желанию."
            )
            return

        price = _digits(parts[1])
        if price is None:
            await message.answer("Цену нужно числом, например 990.")
            return

        category_id = None
        if len(parts) > 2 and parts[2]:
            category_id = await _category_id(storage, parts[2])

        description = parts[3] if len(parts) > 3 else ""
        stock = _digits(parts[4]) if len(parts) > 4 else None

        product_id = await storage.add_product(
            parts[0], price, category_id, description, stock
        )
        await message.answer(
            f"Добавил #{product_id}: <b>{escape(parts[0])}</b> — {money(price, settings)}\n"
            f"Фото: пришлите картинку с подписью <code>/photo {product_id}</code>"
        )

    @router.message(Command("price"))
    async def cmd_price(message: Message, command: CommandObject) -> None:
        args = (command.args or "").split()
        if len(args) < 2 or not args[0].isdigit() or _digits(args[1]) is None:
            await message.answer("Формат: /price 12 1290")
            return

        product = await storage.get_product(int(args[0]))
        if product is None:
            await message.answer("Товар не найден.")
            return

        price = _digits(args[1])
        await storage.update_product(product.id, price=price)
        await message.answer(
            f"<b>{escape(product.title)}</b>: {money(product.price, settings)} → "
            f"<b>{money(price, settings)}</b>"
        )

    @router.message(Command("stock"))
    async def cmd_stock(message: Message, command: CommandObject) -> None:
        args = (command.args or "").split()
        if len(args) < 2 or not args[0].isdigit():
            await message.answer("Формат: /stock 12 5, или /stock 12 - чтобы не считать остаток")
            return

        product = await storage.get_product(int(args[0]))
        if product is None:
            await message.answer("Товар не найден.")
            return

        if args[1] in {"-", "∞", "нет"}:
            await storage.update_product(product.id, stock=None)
            await message.answer(f"<b>{escape(product.title)}</b>: остаток больше не считаю.")
            return

        stock = _digits(args[1])
        if stock is None:
            await message.answer("Остаток нужен числом, например 5.")
            return
        await storage.update_product(product.id, stock=stock)
        await message.answer(f"<b>{escape(product.title)}</b>: осталось {stock} шт.")

    @router.message(F.photo, Command("photo"))
    async def cmd_photo(message: Message, command: CommandObject) -> None:
        raw = (command.args or "").strip()
        product = await storage.get_product(int(raw)) if raw.isdigit() else None
        if product is None:
            await message.answer("Не понял, к какому товару фото. Подпись: /photo 12")
            return

        # Берём самый крупный из вариантов: Telegram отдаёт лесенку размеров.
        file_id = message.photo[-1].file_id
        await storage.update_product(product.id, photo_id=file_id)
        await message.answer(f"Фото для «{escape(product.title)}» сохранил.")

    @router.message(Command("lowstock"))
    async def cmd_lowstock(message: Message) -> None:
        items = await storage.low_stock()
        if not items:
            await message.answer("Всё в достатке.")
            return
        lines = ["<b>Заканчивается</b>", ""]
        lines.extend(f"#{item.id} {escape(item.title)} — {item.stock} шт." for item in items)
        await message.answer("\n".join(lines))

    # ── категории ─────────────────────────────────────────────────────────

    @router.message(Command("categories"))
    async def cmd_categories(message: Message) -> None:
        categories = await storage.list_categories(only_active=False)
        if not categories:
            await message.answer(
                "Категорий нет — каталог показывает все товары одним списком.\n"
                "Добавить: /category_add Аксессуары"
            )
            return
        rows = [
            [
                InlineKeyboardButton(
                    text=f"{'✅' if item.active else '🚫'} {item.title}",
                    callback_data=f"ctoggle:{item.id}",
                )
            ]
            for item in categories
        ]
        await message.answer(
            "<b>Категории</b>\n\nНажмите, чтобы скрыть или вернуть.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @router.callback_query(F.data.startswith("ctoggle:"))
    async def toggle_category(callback: CallbackQuery) -> None:
        raw = payload(callback.data)
        category = await storage.get_category(int(raw)) if raw.isdigit() else None
        if category is None:
            await callback.answer("Категория не найдена")
            return
        await storage.set_category_active(category.id, not category.active)
        await callback.answer("Скрыл" if category.active else "Вернул")

    @router.message(Command("category_add"))
    async def cmd_category_add(message: Message, command: CommandObject) -> None:
        title = (command.args or "").strip()
        if not title:
            await message.answer("Формат: /category_add Аксессуары")
            return
        category_id = await storage.add_category(title)
        await message.answer(
            f"Добавил категорию <b>{escape(title)}</b> (#{category_id}).\n"
            f"Товар в неё: /product_add Чехол | 990 | {escape(title)}"
        )

    # ── статистика ────────────────────────────────────────────────────────

    @router.message(Command("stats"))
    async def cmd_stats(message: Message) -> None:
        stats = await storage.stats(30)
        top = await storage.top_products(30)
        customers = await storage.count_customers()

        lines = [
            "<b>За 30 дней</b>",
            "",
            f"Заказов: {stats['orders']}",
            f"Новых, ждут ответа: {stats['new']}",
            f"Отменено: {stats['cancelled']}",
            f"Выручка: {money(stats['revenue'], settings)}",
            f"Покупателей в базе: {customers}",
        ]
        if top:
            lines.extend(["", "<b>Что покупают</b>"])
            lines.extend(f"• {escape(title)} — {count} шт." for title, count in top)
        await message.answer("\n".join(lines))

    return router


# ──────────────────────────────────────────────────────────────────────────────
# Вспомогательное
# ──────────────────────────────────────────────────────────────────────────────


def _products_keyboard(products, settings: Settings) -> InlineKeyboardMarkup:
    rows = []
    for item in products:
        stock = "" if item.stock is None else f" · {item.stock} шт."
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{'✅' if item.active else '🚫'} #{item.id} {item.title} · "
                    f"{money(item.price, settings)}{stock}",
                    callback_data=f"ptoggle:{item.id}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _category_id(storage: Storage, title: str) -> int | None:
    """Найти категорию по названию или завести новую.

    Продавец пишет название словами; заставлять его помнить числовые id —
    верный способ получить товары не в тех разделах.
    """
    needle = title.strip().lower()
    for category in await storage.list_categories(only_active=False):
        if category.title.lower() == needle:
            return category.id
    return await storage.add_category(title.strip())


def _digits(raw: str) -> int | None:
    value = "".join(char for char in raw if char.isdigit())
    return int(value) if value else None


def admin_commands() -> list[BotCommand]:
    return [
        BotCommand(command="orders", description="Заказы"),
        BotCommand(command="products", description="Товары"),
        BotCommand(command="stats", description="Статистика"),
        BotCommand(command="export", description="Выгрузка заказов"),
        BotCommand(command="admin", description="Справка админа"),
    ]


__all__ = ["ADMIN_HELP", "STATUS_DONE", "admin_commands", "build_admin_router"]
