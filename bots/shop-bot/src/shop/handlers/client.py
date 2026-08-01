"""Витрина покупателя: каталог, корзина, оформление заказа, свои заказы."""

from __future__ import annotations

import logging
import re

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from ..config import Settings
from ..keyboards import (
    BTN_CART,
    BTN_CATALOG,
    BTN_CONTACTS,
    BTN_ORDERS,
    CB_ADD,
    CB_CART,
    CB_CART_DEC,
    CB_CART_INC,
    CB_CATALOG,
    CB_CATEGORY,
    CB_CHECKOUT,
    CB_CLEAR,
    CB_CONFIRM,
    CB_DEC,
    CB_DELIVERY,
    CB_DROP,
    CB_INC,
    CB_NOOP,
    CB_PAGE,
    CB_PRODUCT,
    cart_keyboard,
    categories_keyboard,
    confirm_keyboard,
    delivery_keyboard,
    main_menu,
    page_count,
    payload,
    payment_keyboard,
    product_keyboard,
    products_keyboard,
    request_phone,
    retry_payment_keyboard,
    skip_keyboard,
)
from ..models import DELIVERY_COURIER, DELIVERY_TITLES, PAY_CASH, Cart, Product
from ..notify import Notifier
from ..pricing import below_minimum, totals
from ..storage import EmptyCart, OutOfStock, Storage
from ..texts import (
    cart_text,
    contacts,
    delivery_line,
    escape,
    money,
    order_short,
    order_text,
    payment_prompt,
    product_card,
    welcome,
)

log = logging.getLogger(__name__)

_PHONE_RE = re.compile(r"[\d+()\-\s]{7,20}")
SKIP_WORDS = {"пропустить", "-", "нет", "skip"}


class Checkout(StatesGroup):
    name = State()
    phone = State()
    delivery = State()
    address = State()
    comment = State()
    confirm = State()


def build_client_router(storage: Storage, notifier: Notifier, settings: Settings) -> Router:
    router = Router(name="client")

    # ── меню ──────────────────────────────────────────────────────────────

    @router.message(Command("start"))
    async def cmd_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        if message.from_user:
            await storage.upsert_customer(
                message.from_user.id, username=message.from_user.username
            )
        await message.answer(welcome(settings), reply_markup=main_menu())

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(
            "<b>Как заказать</b>\n\n"
            f"1. {BTN_CATALOG} — выберите товары\n"
            "2. Сложите нужное в корзину кнопкой «🛒 В корзину»\n"
            f"3. {BTN_CART} → «Оформить заказ»\n"
            "4. Оставьте имя, телефон и адрес — заказ уйдёт продавцу\n\n"
            f"{BTN_ORDERS} — статусы ваших заказов\n"
            "/search чехол — поиск по названию",
            reply_markup=main_menu(),
        )

    @router.message(F.text == BTN_CONTACTS)
    async def show_contacts(message: Message) -> None:
        await message.answer(contacts(settings))

    # ── каталог ───────────────────────────────────────────────────────────

    @router.message(F.text == BTN_CATALOG)
    @router.message(Command("catalog"))
    async def show_catalog(message: Message) -> None:
        categories = await storage.list_visible_categories()
        cart = await storage.get_cart(message.from_user.id)

        if not categories:
            # Магазин без категорий — обычное дело для десятка позиций:
            # показываем сразу товары.
            await _send_products(message, storage, settings, cart, category_id=None, page=0)
            return

        await message.answer(
            "🛍 <b>Каталог</b>\n\nВыберите раздел:",
            reply_markup=categories_keyboard(categories, cart),
        )

    @router.callback_query(F.data.startswith(f"{CB_CATALOG}:"))
    async def back_to_catalog(callback: CallbackQuery) -> None:
        categories = await storage.list_visible_categories()
        cart = await storage.get_cart(callback.from_user.id)
        if not categories:
            await _render_products(callback, storage, settings, cart, category_id=None, page=0)
            return
        await _replace(
            callback,
            "🛍 <b>Каталог</b>\n\nВыберите раздел:",
            categories_keyboard(categories, cart),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith(f"{CB_CATEGORY}:"))
    async def open_category(callback: CallbackQuery) -> None:
        raw = payload(callback.data)
        category_id = int(raw) if raw.isdigit() else None
        cart = await storage.get_cart(callback.from_user.id)
        await _render_products(callback, storage, settings, cart, category_id=category_id, page=0)

    @router.callback_query(F.data.startswith(f"{CB_PAGE}:"))
    async def flip_page(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":")
        category_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        page = int(parts[2]) if len(parts) > 2 and parts[2].lstrip("-").isdigit() else 0
        cart = await storage.get_cart(callback.from_user.id)
        await _render_products(
            callback, storage, settings, cart, category_id=category_id or None, page=max(0, page)
        )

    @router.message(Command("search"))
    async def search(message: Message, command: CommandObject) -> None:
        query = (command.args or "").strip()
        if len(query) < 2:
            await message.answer("Что ищем? Например: /search чехол")
            return

        found = await storage.search_products(query)
        if not found:
            await message.answer(f"По запросу «{escape(query)}» ничего не нашлось.")
            return

        cart = await storage.get_cart(message.from_user.id)
        await message.answer(
            f"Нашёл {len(found)} — нажмите, чтобы открыть:",
            reply_markup=products_keyboard(
                found[:10],
                settings,
                category_id=None,
                page=0,
                pages=1,
                cart=cart,
                with_back=False,
            ),
        )

    # ── карточка товара ───────────────────────────────────────────────────

    @router.callback_query(F.data.startswith(f"{CB_PRODUCT}:"))
    async def open_product(callback: CallbackQuery) -> None:
        raw = payload(callback.data)
        product = await storage.get_product(int(raw)) if raw.isdigit() else None
        if product is None or not product.active:
            await callback.answer("Товара больше нет")
            return

        cart = await storage.get_cart(callback.from_user.id)
        line = cart.line(product.id)
        text = product_card(product, settings, in_cart=line.qty if line else 0)
        keyboard = product_keyboard(product, cart, category_id=product.category_id, page=0)

        if product.photo_id:
            # Текстовое сообщение нельзя превратить в фото — старое убираем и
            # шлём карточку с картинкой отдельным сообщением.
            await _drop(callback)
            await callback.bot.send_photo(
                callback.from_user.id, product.photo_id, caption=text, reply_markup=keyboard
            )
        else:
            await _replace(callback, text, keyboard)
        await callback.answer()

    # ── корзина ───────────────────────────────────────────────────────────

    @router.callback_query(F.data.startswith(f"{CB_ADD}:"))
    async def add_to_cart(callback: CallbackQuery) -> None:
        product = await _product_from(callback, storage)
        if product is None:
            return
        if not product.available(track_stock=settings.track_stock):
            await callback.answer("Этого товара сейчас нет в наличии")
            return

        await storage.cart_add(callback.from_user.id, product.id, 1)
        await callback.answer("Добавил в корзину")
        await _refresh_product(callback, storage, settings, product)

    @router.callback_query(F.data.startswith(f"{CB_INC}:"))
    async def increase(callback: CallbackQuery) -> None:
        product = await _change_qty(callback, storage, settings, delta=1)
        if product is not None:
            await _refresh_product(callback, storage, settings, product)

    @router.callback_query(F.data.startswith(f"{CB_DEC}:"))
    async def decrease(callback: CallbackQuery) -> None:
        product = await _change_qty(callback, storage, settings, delta=-1)
        if product is not None:
            await _refresh_product(callback, storage, settings, product)

    @router.callback_query(F.data.startswith(f"{CB_CART_INC}:"))
    async def cart_increase(callback: CallbackQuery) -> None:
        if await _change_qty(callback, storage, settings, delta=1) is not None:
            await _render_cart(callback, storage, settings)

    @router.callback_query(F.data.startswith(f"{CB_CART_DEC}:"))
    async def cart_decrease(callback: CallbackQuery) -> None:
        if await _change_qty(callback, storage, settings, delta=-1) is not None:
            await _render_cart(callback, storage, settings)

    @router.callback_query(F.data.startswith(f"{CB_DROP}:"))
    async def drop_line(callback: CallbackQuery) -> None:
        raw = payload(callback.data)
        if raw.isdigit():
            await storage.cart_remove(callback.from_user.id, int(raw))
        await callback.answer("Убрал")
        await _render_cart(callback, storage, settings)

    @router.callback_query(F.data.startswith(f"{CB_CLEAR}:"))
    async def clear_cart(callback: CallbackQuery) -> None:
        await storage.cart_clear(callback.from_user.id)
        await callback.answer("Корзина пуста")
        await _render_cart(callback, storage, settings)

    @router.message(F.text == BTN_CART)
    @router.message(Command("cart"))
    async def show_cart(message: Message) -> None:
        cart = await storage.get_cart(message.from_user.id)
        await message.answer(cart_text(cart, settings), reply_markup=cart_keyboard(cart))

    @router.callback_query(F.data.startswith(f"{CB_CART}:"))
    async def open_cart(callback: CallbackQuery) -> None:
        await _render_cart(callback, storage, settings)
        await callback.answer()

    # ── оформление ────────────────────────────────────────────────────────

    @router.callback_query(F.data.startswith(f"{CB_CHECKOUT}:"))
    async def start_checkout(callback: CallbackQuery, state: FSMContext) -> None:
        cart = await storage.get_cart(callback.from_user.id)
        if not cart:
            await callback.answer("Корзина пуста")
            return

        short = below_minimum(cart, settings)
        if short:
            await callback.answer(
                f"Минимальный заказ {money(settings.min_order, settings)}. "
                f"Не хватает {money(short, settings)}",
                show_alert=True,
            )
            return

        customer = await storage.get_customer(callback.from_user.id)
        await state.set_state(Checkout.name)
        await state.update_data(saved_name=customer.name if customer else "")

        suggestion = customer.name if customer and customer.name else callback.from_user.full_name
        await _drop(callback)
        await callback.bot.send_message(
            callback.from_user.id,
            f"Как к вам обращаться?\n\nМожно отправить «{escape(suggestion)}» кнопкой ниже.",
            reply_markup=_name_keyboard(suggestion),
        )
        await callback.answer()

    @router.message(StateFilter(Checkout.name), F.text)
    async def got_name(message: Message, state: FSMContext) -> None:
        name = (message.text or "").strip()
        if len(name) < 2:
            await message.answer("Напишите, пожалуйста, имя — так продавцу будет понятнее.")
            return

        await state.update_data(name=name)
        customer = await storage.get_customer(message.from_user.id)
        if customer and customer.phone:
            # Телефон уже знаем — повторно спрашивать невежливо.
            await state.update_data(phone=customer.phone)
            await _ask_delivery(message, state, settings)
            return

        await state.set_state(Checkout.phone)
        await message.answer(
            "Телефон для связи — кнопкой ниже или сообщением.", reply_markup=request_phone()
        )

    @router.message(StateFilter(Checkout.phone), F.contact)
    async def got_contact(message: Message, state: FSMContext) -> None:
        await state.update_data(phone=_normalize_phone(message.contact.phone_number))
        await _ask_delivery(message, state, settings)

    @router.message(StateFilter(Checkout.phone), F.text)
    async def got_phone(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        if not _PHONE_RE.fullmatch(raw) or sum(char.isdigit() for char in raw) < 7:
            await message.answer("Не похоже на номер. Пример: +7 900 123-45-67")
            return
        await state.update_data(phone=_normalize_phone(raw))
        await _ask_delivery(message, state, settings)

    @router.callback_query(StateFilter(Checkout.delivery), F.data.startswith(f"{CB_DELIVERY}:"))
    async def got_delivery(callback: CallbackQuery, state: FSMContext) -> None:
        delivery = payload(callback.data)
        if delivery not in DELIVERY_TITLES:
            await callback.answer("Не понял способ получения")
            return

        await state.update_data(delivery=delivery)
        await callback.answer()

        if delivery != DELIVERY_COURIER:
            await state.update_data(address=settings.pickup_address)
            await _ask_comment(callback.message, state)
            return

        customer = await storage.get_customer(callback.from_user.id)
        await state.set_state(Checkout.address)
        hint = ""
        if customer and customer.address:
            hint = f"\n\nПрошлый адрес: {escape(customer.address)}"
        await _replace(callback, f"Куда доставить? Улица, дом, квартира.{hint}", None)

    @router.message(StateFilter(Checkout.address), F.text)
    async def got_address(message: Message, state: FSMContext) -> None:
        address = (message.text or "").strip()
        if len(address) < 5:
            await message.answer("Напишите адрес подробнее — улица, дом, квартира.")
            return
        await state.update_data(address=address)
        await _ask_comment(message, state)

    @router.message(StateFilter(Checkout.comment), F.text)
    async def got_comment(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        comment = "" if raw.lower() in SKIP_WORDS else raw
        await state.update_data(comment=comment)
        await _show_confirm(message, state, storage, settings)

    @router.callback_query(StateFilter(Checkout.confirm), F.data.startswith(f"{CB_CONFIRM}:"))
    async def confirm_order(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        cart = await storage.get_cart(callback.from_user.id)
        if not cart:
            await callback.answer("Корзина опустела")
            await state.clear()
            return

        delivery = data.get("delivery", "pickup")
        amounts = totals(cart, settings, delivery)

        try:
            order = await storage.create_order(
                customer_id=callback.from_user.id,
                cart=cart,
                delivery=delivery,
                delivery_price=amounts.delivery,
                name=data.get("name", ""),
                phone=data.get("phone", ""),
                address=data.get("address", ""),
                comment=data.get("comment", ""),
                # По умолчанию — при получении: пока покупатель не заплатил
                # картой, заказ считается неоплаченным, а не «ждущим оплату».
                payment_method=PAY_CASH,
                track_stock=settings.track_stock,
            )
        except OutOfStock as exc:
            # Пока покупатель заполнял форму, последнюю единицу купили.
            await callback.answer(f"«{exc}» разобрали, поправьте корзину", show_alert=True)
            await state.clear()
            await _render_cart(callback, storage, settings)
            return
        except EmptyCart:
            await callback.answer("Корзина пуста")
            await state.clear()
            return

        await storage.upsert_customer(
            callback.from_user.id,
            name=order.name,
            phone=order.phone,
            address=order.address if delivery == DELIVERY_COURIER else "",
            username=callback.from_user.username,
        )
        await state.clear()

        keyboard = payment_keyboard(order.id, settings)
        if settings.online_payment_enabled and keyboard is not None:
            # Оплату предлагаем сразу после оформления: в этот момент решимость
            # покупателя максимальная, а через час он уже занят другим.
            await _replace(
                callback,
                f"✅ <b>Заказ №{order.id} принят</b>\n\n"
                + order_text(order, settings)
                + "\n\n"
                + payment_prompt(order, settings),
                keyboard,
            )
        else:
            await _replace(
                callback,
                f"✅ <b>Заказ №{order.id} принят</b>\n\n"
                + order_text(order, settings)
                + "\n\nПродавец свяжется с вами для подтверждения.",
                None,
            )
        await callback.answer("Заказ отправлен")
        await notifier.new_order(order)

    # ── свои заказы ───────────────────────────────────────────────────────

    @router.message(F.text == BTN_ORDERS)
    @router.message(Command("orders"))
    async def my_orders(message: Message) -> None:
        orders = await storage.list_orders(customer_id=message.from_user.id, limit=10)
        if not orders:
            await message.answer("Заказов пока нет.", reply_markup=main_menu())
            return

        lines = ["📦 <b>Ваши заказы</b>", ""]
        lines.extend(order_short(item, settings) for item in orders)
        lines.append("")
        lines.append("Подробности последнего:")
        await message.answer("\n".join(lines))

        last = orders[0]
        keyboard = None
        if last.awaits_payment and settings.online_payment_enabled:
            # Оплата не прошла или её отложили — дать возможность вернуться к ней.
            keyboard = retry_payment_keyboard(last.id, settings)
        await message.answer(order_text(last, settings), reply_markup=keyboard)

    @router.callback_query(F.data.startswith(f"{CB_NOOP}:"))
    async def noop(callback: CallbackQuery) -> None:
        await callback.answer()

    return router


# ──────────────────────────────────────────────────────────────────────────────
# Отрисовка
# ──────────────────────────────────────────────────────────────────────────────


async def _page(
    storage: Storage, settings: Settings, category_id: int | None, page: int
) -> tuple[list[Product], int, int]:
    total = await storage.count_products(category_id)
    pages = max(1, page_count(total, settings.page_size))
    page = min(max(0, page), pages - 1)
    products = await storage.list_products(
        category_id, limit=settings.page_size, offset=page * settings.page_size
    )
    return products, page, pages


def _page_text(title: str, products: list[Product]) -> str:
    if not products:
        return "Здесь пока пусто. Загляните позже."
    lines = [f"<b>{escape(title)}</b>", ""]
    lines.append("Нажмите на товар, чтобы посмотреть подробности.")
    return "\n".join(lines)


async def _send_products(
    message: Message,
    storage: Storage,
    settings: Settings,
    cart: Cart,
    *,
    category_id: int | None,
    page: int,
) -> None:
    products, page, pages = await _page(storage, settings, category_id, page)
    title = await _title(storage, settings, category_id)
    await message.answer(
        _page_text(title, products),
        reply_markup=products_keyboard(
            products,
            settings,
            category_id=category_id,
            page=page,
            pages=pages,
            cart=cart,
            with_back=bool(await storage.list_visible_categories()),
        ),
    )


async def _render_products(
    callback: CallbackQuery,
    storage: Storage,
    settings: Settings,
    cart: Cart,
    *,
    category_id: int | None,
    page: int,
) -> None:
    products, page, pages = await _page(storage, settings, category_id, page)
    title = await _title(storage, settings, category_id)
    await _replace(
        callback,
        _page_text(title, products),
        products_keyboard(
            products,
            settings,
            category_id=category_id,
            page=page,
            pages=pages,
            cart=cart,
            with_back=bool(await storage.list_visible_categories()),
        ),
    )
    await callback.answer()


async def _title(storage: Storage, settings: Settings, category_id: int | None) -> str:
    if category_id is None:
        return settings.shop_name
    category = await storage.get_category(category_id)
    return category.title if category else settings.shop_name


async def _render_cart(callback: CallbackQuery, storage: Storage, settings: Settings) -> None:
    cart = await storage.get_cart(callback.from_user.id)
    await _replace(callback, cart_text(cart, settings), cart_keyboard(cart))


async def _refresh_product(
    callback: CallbackQuery, storage: Storage, settings: Settings, product: Product
) -> None:
    """Перерисовать карточку после изменения количества."""
    cart = await storage.get_cart(callback.from_user.id)
    line = cart.line(product.id)
    text = product_card(product, settings, in_cart=line.qty if line else 0)
    keyboard = product_keyboard(product, cart, category_id=product.category_id, page=0)

    if not isinstance(callback.message, Message):
        return
    try:
        if callback.message.photo:
            await callback.message.edit_caption(caption=text, reply_markup=keyboard)
        else:
            await callback.message.edit_text(text, reply_markup=keyboard)
    except Exception:
        # Сообщение могло не измениться (двойное нажатие) — это не ошибка.
        log.debug("карточка не обновилась", exc_info=True)


async def _change_qty(
    callback: CallbackQuery, storage: Storage, settings: Settings, *, delta: int
) -> Product | None:
    product = await _product_from(callback, storage)
    if product is None:
        return None

    cart = await storage.get_cart(callback.from_user.id)
    line = cart.line(product.id)
    current = line.qty if line else 0
    target = current + delta

    limit = product.max_qty(track_stock=settings.track_stock)
    if target > limit:
        await callback.answer(f"Больше нет: осталось {limit} шт.")
        return None

    await storage.cart_set_qty(callback.from_user.id, product.id, target)
    await callback.answer("Убрал из корзины" if target <= 0 else f"{target} шт.")
    return product


async def _product_from(callback: CallbackQuery, storage: Storage) -> Product | None:
    raw = payload(callback.data)
    product = await storage.get_product(int(raw)) if raw.isdigit() else None
    if product is None:
        await callback.answer("Товар не найден")
        return None
    return product


# ── шаги оформления ───────────────────────────────────────────────────────


async def _ask_delivery(message: Message, state: FSMContext, settings: Settings) -> None:
    keyboard = delivery_keyboard(settings)
    if not keyboard.inline_keyboard:
        # Ни самовывоза, ни доставки в настройках — считаем это самовывозом,
        # иначе оформление упирается в пустой экран.
        await state.update_data(delivery="pickup", address=settings.pickup_address)
        await _ask_comment(message, state)
        return

    await state.set_state(Checkout.delivery)
    await message.answer("Как удобнее получить заказ?", reply_markup=ReplyKeyboardRemove())
    await message.answer("Выберите способ:", reply_markup=keyboard)


async def _ask_comment(message: Message, state: FSMContext) -> None:
    await state.set_state(Checkout.comment)
    await message.answer(
        "Комментарий к заказу — время, подъезд, пожелания.\n"
        "Если ничего не нужно, нажмите «Пропустить».",
        reply_markup=skip_keyboard(),
    )


async def _show_confirm(
    message: Message, state: FSMContext, storage: Storage, settings: Settings
) -> None:
    data = await state.get_data()
    cart = await storage.get_cart(message.from_user.id)
    if not cart:
        await message.answer("Корзина опустела.", reply_markup=main_menu())
        await state.clear()
        return

    delivery = data.get("delivery", "pickup")
    amounts = totals(cart, settings, delivery)

    lines = [cart_text(cart, settings), "", "<b>Получение</b>"]
    lines.append(f"Способ: {DELIVERY_TITLES.get(delivery, delivery)}")
    if data.get("address"):
        lines.append(f"📍 {escape(data['address'])}")
    lines.append(delivery_line(amounts, settings))
    lines.append(f"<b>К оплате: {money(amounts.total, settings)}</b>")
    lines.append("")
    lines.append(f"👤 {escape(data.get('name', ''))} · {escape(data.get('phone', ''))}")
    if data.get("comment"):
        lines.append(f"💬 {escape(data['comment'])}")

    await state.set_state(Checkout.confirm)
    await message.answer("Проверьте заказ:", reply_markup=main_menu())
    await message.answer("\n".join(lines), reply_markup=confirm_keyboard())


def _name_keyboard(suggestion: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=suggestion[:64])]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


# ── работа с сообщениями ──────────────────────────────────────────────────


async def _replace(callback: CallbackQuery, text: str, keyboard) -> None:
    """Заменить сообщение под кнопкой; фото-карточку — убрать и прислать новое."""
    if not isinstance(callback.message, Message):
        return
    if callback.message.photo:
        await _drop(callback)
        await callback.message.answer(text, reply_markup=keyboard)
        return
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except Exception:
        await callback.message.answer(text, reply_markup=keyboard)


async def _drop(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        try:
            await callback.message.delete()
        except Exception:
            # Старше 48 часов Telegram удалять не даёт — не беда.
            log.debug("не удалил сообщение", exc_info=True)


def _normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in "78":
        return f"+7 {digits[1:4]} {digits[4:7]}-{digits[7:9]}-{digits[9:]}"
    if len(digits) == 10:
        return f"+7 {digits[0:3]} {digits[3:6]}-{digits[6:8]}-{digits[8:]}"
    return raw.strip()
