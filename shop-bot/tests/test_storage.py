from __future__ import annotations

import pytest

from shop.models import PAY_CASH, PAY_ONLINE, STATUS_ACCEPTED, STATUS_CANCELLED
from shop.storage import EmptyCart, OutOfStock, Storage


async def add(storage: Storage, title: str = "Чехол", price: int = 990, stock=None) -> int:
    return await storage.add_product(title, price, stock=stock)


# ── каталог ───────────────────────────────────────────────────────────────


async def test_hidden_and_sold_out_products_are_not_shown(storage: Storage):
    visible = await add(storage, "В наличии", stock=5)
    await add(storage, "Распродан", stock=0)
    hidden = await add(storage, "Скрытый")
    await storage.update_product(hidden, active=0)

    listed = await storage.list_products()
    assert [item.id for item in listed] == [visible]
    # Без учёта доступности видно всё — это админский список.
    assert len(await storage.list_products(only_available=False)) == 3


async def test_products_without_stock_tracking_are_always_available(storage: Storage):
    await add(storage, "Печать под заказ", stock=None)
    assert len(await storage.list_products()) == 1


async def test_pagination(storage: Storage):
    for index in range(5):
        await add(storage, f"Товар {index}")

    assert await storage.count_products() == 5
    page = await storage.list_products(limit=2, offset=2)
    assert [item.title for item in page] == ["Товар 2", "Товар 3"]


async def test_empty_categories_are_hidden_from_the_storefront(storage: Storage):
    filled = await storage.add_category("С товарами")
    await storage.add_category("Пустая")
    await storage.add_product("Чехол", 990, filled)

    visible = await storage.list_visible_categories()
    assert [item.id for item in visible] == [filled]


async def test_search_looks_into_title_and_description(storage: Storage):
    await storage.add_product("Чехол силиконовый", 990, description="чёрный, матовый")
    await storage.add_product("Кружка", 690, description="керамика")

    assert len(await storage.search_products("чехол")) == 1
    assert len(await storage.search_products("керамик")) == 1
    assert await storage.search_products("ноутбук") == []


# ── корзина ───────────────────────────────────────────────────────────────


async def test_cart_add_accumulates(storage: Storage):
    product_id = await add(storage)
    await storage.cart_add(1, product_id)
    assert await storage.cart_add(1, product_id, 2) == 3

    cart = await storage.get_cart(1)
    assert cart.count == 3
    assert cart.subtotal == 990 * 3


async def test_carts_are_per_user(storage: Storage):
    product_id = await add(storage)
    await storage.cart_add(1, product_id, 2)
    await storage.cart_add(2, product_id, 1)

    assert (await storage.get_cart(1)).count == 2
    assert (await storage.get_cart(2)).count == 1


async def test_setting_qty_to_zero_removes_the_line(storage: Storage):
    product_id = await add(storage)
    await storage.cart_add(1, product_id, 3)
    await storage.cart_set_qty(1, product_id, 0)
    assert not await storage.get_cart(1)


async def test_hidden_product_drops_out_of_the_cart(storage: Storage):
    """Продавец снял позицию с продажи — покупатель не должен уйти с ней на кассу."""
    product_id = await add(storage)
    await storage.cart_add(1, product_id, 2)
    await storage.update_product(product_id, active=0)

    assert not await storage.get_cart(1)


# ── заказы ────────────────────────────────────────────────────────────────


async def test_order_copies_titles_and_prices(storage: Storage):
    product_id = await add(storage, "Чехол", 990)
    await storage.cart_add(1, product_id, 2)
    cart = await storage.get_cart(1)

    order = await storage.create_order(
        customer_id=1,
        cart=cart,
        delivery="courier",
        delivery_price=300,
        name="Иван",
        phone="+7 900 000-00-00",
        address="Ленина, 1",
    )

    assert order.goods_total == 1980
    assert order.total == 2280
    assert order.lines[0].title == "Чехол"

    # Цена изменилась — в заказе остаётся та, по которой покупали.
    await storage.update_product(product_id, price=1290, title="Чехол новый")
    again = await storage.get_order(order.id)
    assert again.lines[0].price == 990
    assert again.lines[0].title == "Чехол"


async def test_order_empties_the_cart(storage: Storage):
    product_id = await add(storage)
    await storage.cart_add(1, product_id, 1)
    await storage.create_order(
        customer_id=1,
        cart=await storage.get_cart(1),
        delivery="pickup",
        delivery_price=0,
        name="Иван",
        phone="+7 900 000-00-00",
    )
    assert not await storage.get_cart(1)


async def test_order_decrements_stock(storage: Storage):
    product_id = await add(storage, stock=5)
    await storage.cart_add(1, product_id, 2)
    await storage.create_order(
        customer_id=1,
        cart=await storage.get_cart(1),
        delivery="pickup",
        delivery_price=0,
        name="Иван",
        phone="+7 900 000-00-00",
    )
    assert (await storage.get_product(product_id)).stock == 3


async def test_order_fails_when_stock_ran_out(storage: Storage):
    """Пока покупатель заполнял форму, последнюю единицу купили."""
    product_id = await add(storage, "Последний", stock=2)
    await storage.cart_add(1, product_id, 2)
    cart = await storage.get_cart(1)

    await storage.update_product(product_id, stock=1)

    with pytest.raises(OutOfStock) as exc:
        await storage.create_order(
            customer_id=1,
            cart=cart,
            delivery="pickup",
            delivery_price=0,
            name="Иван",
            phone="+7 900 000-00-00",
        )
    assert "Последний" in str(exc.value)
    # Транзакция откатилась целиком: ни заказа, ни списанного остатка.
    assert (await storage.get_product(product_id)).stock == 1
    assert await storage.list_orders() == []


async def test_untracked_stock_never_blocks_an_order(storage: Storage):
    product_id = await add(storage, stock=None)
    await storage.cart_add(1, product_id, 50)
    order = await storage.create_order(
        customer_id=1,
        cart=await storage.get_cart(1),
        delivery="pickup",
        delivery_price=0,
        name="Иван",
        phone="+7 900 000-00-00",
    )
    assert order.lines[0].qty == 50
    assert (await storage.get_product(product_id)).stock is None


async def test_empty_cart_cannot_be_ordered(storage: Storage):
    with pytest.raises(EmptyCart):
        await storage.create_order(
            customer_id=1,
            cart=await storage.get_cart(1),
            delivery="pickup",
            delivery_price=0,
            name="Иван",
            phone="+7 900 000-00-00",
        )


async def test_cancelling_returns_stock(storage: Storage):
    product_id = await add(storage, stock=5)
    await storage.cart_add(1, product_id, 2)
    order = await storage.create_order(
        customer_id=1,
        cart=await storage.get_cart(1),
        delivery="pickup",
        delivery_price=0,
        name="Иван",
        phone="+7 900 000-00-00",
    )

    cancelled = await storage.set_order_status(order.id, STATUS_CANCELLED)
    assert cancelled.status == STATUS_CANCELLED
    assert (await storage.get_product(product_id)).stock == 5

    # Повторная отмена ничего не меняет — иначе остаток вырос бы на пустом месте.
    assert await storage.set_order_status(order.id, STATUS_CANCELLED) is None
    assert (await storage.get_product(product_id)).stock == 5


async def test_accepting_does_not_touch_stock(storage: Storage):
    product_id = await add(storage, stock=5)
    await storage.cart_add(1, product_id, 2)
    order = await storage.create_order(
        customer_id=1,
        cart=await storage.get_cart(1),
        delivery="pickup",
        delivery_price=0,
        name="Иван",
        phone="+7 900 000-00-00",
    )
    await storage.set_order_status(order.id, STATUS_ACCEPTED)
    assert (await storage.get_product(product_id)).stock == 3


# ── покупатели и статистика ───────────────────────────────────────────────


async def test_customer_fields_are_not_erased_by_empty_values(storage: Storage):
    await storage.upsert_customer(1, name="Иван", phone="+7 900 000-00-00", address="Ленина, 1")
    await storage.upsert_customer(1, address="Мира, 5")

    customer = await storage.get_customer(1)
    assert customer.name == "Иван"
    assert customer.phone == "+7 900 000-00-00"
    assert customer.address == "Мира, 5"


async def test_stats_exclude_cancelled_from_revenue(storage: Storage):
    product_id = await add(storage, price=1000)
    for customer_id in (1, 2):
        await storage.cart_add(customer_id, product_id, 1)
        await storage.create_order(
            customer_id=customer_id,
            cart=await storage.get_cart(customer_id),
            delivery="pickup",
            delivery_price=0,
            name="Иван",
            phone="+7 900 000-00-00",
        )

    orders = await storage.list_orders()
    await storage.set_order_status(orders[0].id, STATUS_CANCELLED)

    stats = await storage.stats(30)
    assert stats["orders"] == 2
    assert stats["cancelled"] == 1
    assert stats["revenue"] == 1000
    assert await storage.top_products(30) == [("Чехол", 1)]


async def test_low_stock_lists_what_is_running_out(storage: Storage):
    await add(storage, "Заканчивается", stock=2)
    await add(storage, "Хватает", stock=50)
    await add(storage, "Без учёта", stock=None)

    low = await storage.low_stock(threshold=3)
    assert [item.title for item in low] == ["Заканчивается"]


# ── оплата ────────────────────────────────────────────────────────────────


async def make_order(storage: Storage, **kwargs):
    product_id = await add(storage)
    await storage.cart_add(1, product_id, 1)
    return await storage.create_order(
        customer_id=1,
        cart=await storage.get_cart(1),
        delivery="pickup",
        delivery_price=0,
        name="Иван",
        phone="+7 900 000-00-00",
        **kwargs,
    )


async def test_new_order_is_unpaid_and_pays_on_delivery(storage: Storage):
    order = await make_order(storage)
    assert order.payment_method == PAY_CASH
    assert not order.is_paid
    assert order.paid_at is None


async def test_payment_method_can_be_switched_before_paying(storage: Storage):
    order = await make_order(storage)

    updated = await storage.set_payment_method(order.id, PAY_ONLINE)
    assert updated.payment_method == PAY_ONLINE
    assert updated.awaits_payment


async def test_mark_paid_records_the_charge(storage: Storage):
    order = await make_order(storage)
    paid = await storage.mark_paid(order.id, "charge-123")

    assert paid.is_paid
    assert paid.charge_id == "charge-123"
    assert paid.paid_at is not None
    # Оплата картой сама означает выбранный способ.
    assert paid.payment_method == PAY_ONLINE
    assert not paid.awaits_payment


async def test_second_payment_notification_changes_nothing(storage: Storage):
    """successful_payment может прийти повторно после сбоя сети."""
    order = await make_order(storage)
    await storage.mark_paid(order.id, "charge-123")

    assert await storage.mark_paid(order.id, "charge-456") is None
    assert (await storage.get_order(order.id)).charge_id == "charge-123"


async def test_paid_order_keeps_its_payment_method(storage: Storage):
    order = await make_order(storage)
    await storage.mark_paid(order.id, "charge-123")

    assert await storage.set_payment_method(order.id, PAY_CASH) is None
    assert (await storage.get_order(order.id)).payment_method == PAY_ONLINE


async def test_unpaid_online_orders_wait_before_showing_up(storage: Storage):
    order = await make_order(storage)
    await storage.set_payment_method(order.id, PAY_ONLINE)

    # Только что оформленный заказ ещё не «зависший» — покупатель платит.
    assert await storage.unpaid_online_orders(older_than_minutes=15) == []
    assert len(await storage.unpaid_online_orders(older_than_minutes=0)) == 1

    await storage.mark_paid(order.id, "charge-123")
    assert await storage.unpaid_online_orders(older_than_minutes=0) == []


async def test_cancelled_order_is_not_waiting_for_payment(storage: Storage):
    order = await make_order(storage)
    await storage.set_payment_method(order.id, PAY_ONLINE)
    await storage.set_order_status(order.id, STATUS_CANCELLED)

    assert await storage.unpaid_online_orders(older_than_minutes=0) == []


async def test_stats_separate_paid_money_from_total_revenue(storage: Storage):
    first = await make_order(storage)
    await make_order(storage)
    await storage.mark_paid(first.id, "charge-123")

    stats = await storage.stats(30)
    assert stats["orders"] == 2
    assert stats["revenue"] == 1980  # оба заказа по 990
    assert stats["paid_orders"] == 1
    assert stats["paid_revenue"] == 990


async def test_old_database_gets_the_payment_columns(tmp_path):
    """У продавца, который уже принимает заказы, база создана без этих колонок."""
    import aiosqlite

    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as db:
        await db.executescript(
            """
            CREATE TABLE orders (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id    INTEGER NOT NULL,
                goods_total    INTEGER NOT NULL,
                delivery_price INTEGER NOT NULL DEFAULT 0,
                delivery       TEXT NOT NULL DEFAULT 'pickup',
                address        TEXT NOT NULL DEFAULT '',
                name           TEXT NOT NULL DEFAULT '',
                phone          TEXT NOT NULL DEFAULT '',
                comment        TEXT NOT NULL DEFAULT '',
                status         TEXT NOT NULL DEFAULT 'new',
                created_at     TEXT NOT NULL
            );
            INSERT INTO orders (customer_id, goods_total, created_at)
            VALUES (1, 990, '2026-07-01T10:00:00+00:00');
            """
        )
        await db.commit()

    async with Storage(str(path)) as storage:
        order = await storage.get_order(1)
        # Старый заказ цел, а новые поля получили значения по умолчанию.
        assert order is not None
        assert order.goods_total == 990
        assert order.payment_method == PAY_CASH
        assert not order.is_paid

        paid = await storage.mark_paid(1, "charge-1")
        assert paid.is_paid
