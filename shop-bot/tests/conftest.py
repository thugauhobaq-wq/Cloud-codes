from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from shop.config import Settings
from shop.models import Cart, CartLine, Product
from shop.storage import Storage


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        bot_token="test",
        owner_id=1,
        db_path=str(tmp_path / "test.db"),
        shop_name="Тестовый магазин",
        pickup_address="Ленина, 1",
        delivery_price=300,
        free_delivery_from=3000,
        min_order=500,
        page_size=2,
    )


@pytest_asyncio.fixture
async def storage(settings: Settings) -> AsyncIterator[Storage]:
    store = Storage(settings.db_path)
    await store.open()
    try:
        yield store
    finally:
        await store.close()


def product(
    product_id: int = 1,
    price: int = 1000,
    *,
    title: str = "Товар",
    stock: int | None = None,
    active: bool = True,
) -> Product:
    return Product(id=product_id, title=title, price=price, stock=stock, active=active)


def cart(*lines: tuple[Product, int]) -> Cart:
    return Cart(lines=[CartLine(product=item, qty=qty) for item, qty in lines])
