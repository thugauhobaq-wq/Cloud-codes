from __future__ import annotations

import pytest

from shop.models import STATUS_ACCEPTED, STATUS_CANCELLED
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
