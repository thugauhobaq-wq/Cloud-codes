"""Хранилище: каталог, корзины, заказы, покупатели.

Корзина лежит в базе, а не в памяти процесса: покупатель складывает товары
неделю, а бота за это время перезапустят не раз.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from .models import (
    STATUS_CANCELLED,
    STATUS_NEW,
    Cart,
    CartLine,
    Category,
    Customer,
    Order,
    OrderLine,
    Product,
)

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    title    TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    active   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS products (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER REFERENCES categories (id) ON DELETE SET NULL,
    title       TEXT NOT NULL,
    price       INTEGER NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    photo_id    TEXT NOT NULL DEFAULT '',
    -- NULL: товар без учёта остатков (услуга, печать под заказ).
    stock       INTEGER,
    active      INTEGER NOT NULL DEFAULT 1,
    position    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS products_category_idx ON products (category_id, active);

CREATE TABLE IF NOT EXISTS cart_items (
    user_id    INTEGER NOT NULL,
    product_id INTEGER NOT NULL REFERENCES products (id) ON DELETE CASCADE,
    qty        INTEGER NOT NULL,
    added_at   TEXT NOT NULL,
    PRIMARY KEY (user_id, product_id)
);

CREATE TABLE IF NOT EXISTS customers (
    tg_id    INTEGER PRIMARY KEY,
    name     TEXT NOT NULL DEFAULT '',
    phone    TEXT NOT NULL DEFAULT '',
    address  TEXT NOT NULL DEFAULT '',
    username TEXT
);

CREATE TABLE IF NOT EXISTS orders (
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
CREATE INDEX IF NOT EXISTS orders_status_idx ON orders (status, created_at);
CREATE INDEX IF NOT EXISTS orders_customer_idx ON orders (customer_id, created_at);

-- Название и цена скопированы: товар потом подорожает, а в заказе должно
-- остаться то, что видел покупатель.
CREATE TABLE IF NOT EXISTS order_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   INTEGER NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    product_id INTEGER,
    title      TEXT NOT NULL,
    price      INTEGER NOT NULL,
    qty        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS order_items_order_idx ON order_items (order_id);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class OutOfStock(RuntimeError):
    """Товара не хватило на момент оформления. Внутри — название."""


class EmptyCart(RuntimeError):
    """Оформлять нечего."""


class Storage:
    def __init__(self, db_path: str) -> None:
        self._path = Path(db_path)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # ── жизненный цикл ────────────────────────────────────────────────────

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        # Встроенный lower() в SQLite умеет только латиницу: «Чехол» так и
        # остаётся «Чехол», и поиск по-русски не находит ничего.
        await self._db.create_function("rulower", 1, _rulower, deterministic=True)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> Storage:
        await self.open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Storage.open() не вызван")
        return self._db

    # ── категории ─────────────────────────────────────────────────────────

    async def list_categories(self, *, only_active: bool = True) -> list[Category]:
        sql = "SELECT * FROM categories"
        if only_active:
            sql += " WHERE active = 1"
        sql += " ORDER BY position, id"
        async with self._lock, self._conn.execute(sql) as cursor:
            return [_category(row) for row in await cursor.fetchall()]

    async def list_visible_categories(self) -> list[Category]:
        """Категории, в которых есть что показать.

        Пустая категория в витрине — тупик: покупатель заходит и видит
        «здесь пока ничего нет».
        """
        async with self._lock, self._conn.execute(
            "SELECT c.* FROM categories c"
            " WHERE c.active = 1 AND EXISTS ("
            "   SELECT 1 FROM products p"
            "   WHERE p.category_id = c.id AND p.active = 1"
            "     AND (p.stock IS NULL OR p.stock > 0))"
            " ORDER BY c.position, c.id"
        ) as cursor:
            return [_category(row) for row in await cursor.fetchall()]

    async def get_category(self, category_id: int) -> Category | None:
        async with self._lock, self._conn.execute(
            "SELECT * FROM categories WHERE id = ?", (category_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return _category(row) if row else None

    async def add_category(self, title: str) -> int:
        async with self._lock:
            async with self._conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM categories"
            ) as cursor:
                position = (await cursor.fetchone())[0]
            cursor = await self._conn.execute(
                "INSERT INTO categories (title, position) VALUES (?, ?)", (title, position)
            )
            await self._conn.commit()
            return int(cursor.lastrowid)

    async def set_category_active(self, category_id: int, active: bool) -> None:
        async with self._lock:
            await self._conn.execute(
                "UPDATE categories SET active = ? WHERE id = ?", (int(active), category_id)
            )
            await self._conn.commit()

    # ── товары ────────────────────────────────────────────────────────────

    async def list_products(
        self,
        category_id: int | None = None,
        *,
        only_available: bool = True,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Product]:
        sql = _PRODUCT_SELECT
        conditions: list[str] = []
        params: list = []
        if category_id is not None:
            conditions.append("p.category_id = ?")
            params.append(category_id)
        if only_available:
            conditions.append("p.active = 1 AND (p.stock IS NULL OR p.stock > 0)")
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY p.position, p.id"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])

        async with self._lock, self._conn.execute(sql, params) as cursor:
            return [_product(row) for row in await cursor.fetchall()]

    async def count_products(
        self, category_id: int | None = None, *, only_available: bool = True
    ) -> int:
        sql = "SELECT COUNT(*) FROM products p"
        conditions: list[str] = []
        params: list = []
        if category_id is not None:
            conditions.append("p.category_id = ?")
            params.append(category_id)
        if only_available:
            conditions.append("p.active = 1 AND (p.stock IS NULL OR p.stock > 0)")
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        async with self._lock, self._conn.execute(sql, params) as cursor:
            return int((await cursor.fetchone())[0])

    async def search_products(self, query: str, limit: int = 20) -> list[Product]:
        pattern = f"%{query.strip().lower()}%"
        async with self._lock, self._conn.execute(
            _PRODUCT_SELECT
            + " WHERE p.active = 1 AND (p.stock IS NULL OR p.stock > 0)"
            "   AND (rulower(p.title) LIKE ? OR rulower(p.description) LIKE ?)"
            " ORDER BY p.position, p.id LIMIT ?",
            (pattern, pattern, limit),
        ) as cursor:
            return [_product(row) for row in await cursor.fetchall()]

    async def get_product(self, product_id: int) -> Product | None:
        async with self._lock, self._conn.execute(
            _PRODUCT_SELECT + " WHERE p.id = ?", (product_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return _product(row) if row else None

    async def add_product(
        self,
        title: str,
        price: int,
        category_id: int | None = None,
        description: str = "",
        stock: int | None = None,
    ) -> int:
        async with self._lock:
            async with self._conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM products"
            ) as cursor:
                position = (await cursor.fetchone())[0]
            cursor = await self._conn.execute(
                "INSERT INTO products (category_id, title, price, description, stock, position)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (category_id, title, price, description, stock, position),
            )
            await self._conn.commit()
            return int(cursor.lastrowid)

    async def update_product(self, product_id: int, **fields) -> None:
        allowed = {
            "title",
            "price",
            "description",
            "photo_id",
            "stock",
            "active",
            "position",
            "category_id",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        async with self._lock:
            await self._conn.execute(
                f"UPDATE products SET {assignments} WHERE id = ?",
                (*updates.values(), product_id),
            )
            await self._conn.commit()

    # ── корзина ───────────────────────────────────────────────────────────

    async def get_cart(self, user_id: int) -> Cart:
        """Корзина покупателя.

        Скрытые и распроданные товары из выдачи выпадают сами: если продавец
        снял позицию с продажи, покупатель не должен уйти с ней на кассу.
        """
        async with self._lock, self._conn.execute(
            "SELECT p.*, COALESCE(c.title, '') AS category_title, ci.qty AS qty"
            " FROM cart_items ci"
            " JOIN products p ON p.id = ci.product_id"
            " LEFT JOIN categories c ON c.id = p.category_id"
            " WHERE ci.user_id = ? AND p.active = 1"
            " ORDER BY ci.added_at",
            (user_id,),
        ) as cursor:
            rows = await cursor.fetchall()

        return Cart(
            lines=[CartLine(product=_product(row), qty=int(row["qty"])) for row in rows]
        )

    async def cart_add(self, user_id: int, product_id: int, qty: int = 1) -> int:
        """Добавить в корзину, вернуть новое количество."""
        async with self._lock:
            await self._conn.execute(
                "INSERT INTO cart_items (user_id, product_id, qty, added_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (user_id, product_id) DO UPDATE SET qty = qty + excluded.qty",
                (user_id, product_id, qty, datetime.now(UTC).isoformat()),
            )
            await self._conn.commit()
            async with self._conn.execute(
                "SELECT qty FROM cart_items WHERE user_id = ? AND product_id = ?",
                (user_id, product_id),
            ) as cursor:
                row = await cursor.fetchone()
        return int(row["qty"]) if row else 0

    async def cart_set_qty(self, user_id: int, product_id: int, qty: int) -> None:
        if qty <= 0:
            await self.cart_remove(user_id, product_id)
            return
        async with self._lock:
            await self._conn.execute(
                "UPDATE cart_items SET qty = ? WHERE user_id = ? AND product_id = ?",
                (qty, user_id, product_id),
            )
            await self._conn.commit()

    async def cart_remove(self, user_id: int, product_id: int) -> None:
        async with self._lock:
            await self._conn.execute(
                "DELETE FROM cart_items WHERE user_id = ? AND product_id = ?",
                (user_id, product_id),
            )
            await self._conn.commit()

    async def cart_clear(self, user_id: int) -> None:
        async with self._lock:
            await self._conn.execute("DELETE FROM cart_items WHERE user_id = ?", (user_id,))
            await self._conn.commit()

    # ── покупатели ────────────────────────────────────────────────────────

    async def upsert_customer(
        self,
        tg_id: int,
        *,
        name: str = "",
        phone: str = "",
        address: str = "",
        username: str | None = None,
    ) -> None:
        """Запомнить данные покупателя.

        Пустые значения не затирают сохранённые: при повторном заказе человек
        часто меняет только адрес, и терять из-за этого телефон незачем.
        """
        async with self._lock:
            await self._conn.execute(
                "INSERT INTO customers (tg_id, name, phone, address, username)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (tg_id) DO UPDATE SET"
                "   name = CASE WHEN excluded.name <> ''"
                "        THEN excluded.name ELSE customers.name END,"
                "   phone = CASE WHEN excluded.phone <> ''"
                "        THEN excluded.phone ELSE customers.phone END,"
                "   address = CASE WHEN excluded.address <> ''"
                "        THEN excluded.address ELSE customers.address END,"
                "   username = excluded.username",
                (tg_id, name, phone, address, username),
            )
            await self._conn.commit()

    async def get_customer(self, tg_id: int) -> Customer | None:
        async with self._lock, self._conn.execute(
            "SELECT * FROM customers WHERE tg_id = ?", (tg_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return Customer(
            tg_id=int(row["tg_id"]),
            name=row["name"],
            phone=row["phone"],
            address=row["address"],
            username=row["username"],
        )

    async def count_customers(self) -> int:
        async with self._lock, self._conn.execute("SELECT COUNT(*) FROM customers") as cursor:
            return int((await cursor.fetchone())[0])

    # ── заказы ────────────────────────────────────────────────────────────

    async def create_order(
        self,
        *,
        customer_id: int,
        cart: Cart,
        delivery: str,
        delivery_price: int,
        name: str,
        phone: str,
        address: str = "",
        comment: str = "",
        track_stock: bool = True,
    ) -> Order:
        """Оформить заказ и списать остатки — одной транзакцией.

        Между «положил в корзину» и «подтвердил» проходят часы, за которые
        последнюю единицу успевают купить. Проверка и списание врозь означали
        бы проданный дважды товар.
        """
        if not cart.lines:
            raise EmptyCart

        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                if track_stock:
                    for line in cart.lines:
                        async with self._conn.execute(
                            "SELECT title, stock, active FROM products WHERE id = ?",
                            (line.product.id,),
                        ) as cursor:
                            row = await cursor.fetchone()
                        if row is None or not row["active"]:
                            raise OutOfStock(line.product.title)
                        if row["stock"] is not None and row["stock"] < line.qty:
                            raise OutOfStock(row["title"])

                    for line in cart.lines:
                        await self._conn.execute(
                            "UPDATE products SET stock = stock - ?"
                            " WHERE id = ? AND stock IS NOT NULL",
                            (line.qty, line.product.id),
                        )

                cursor = await self._conn.execute(
                    "INSERT INTO orders"
                    " (customer_id, goods_total, delivery_price, delivery, address,"
                    "  name, phone, comment, status, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        customer_id,
                        cart.subtotal,
                        delivery_price,
                        delivery,
                        address,
                        name,
                        phone,
                        comment,
                        STATUS_NEW,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                order_id = int(cursor.lastrowid)
                await self._conn.executemany(
                    "INSERT INTO order_items (order_id, product_id, title, price, qty)"
                    " VALUES (?, ?, ?, ?, ?)",
                    [
                        (
                            order_id,
                            line.product.id,
                            line.product.title,
                            line.product.price,
                            line.qty,
                        )
                        for line in cart.lines
                    ],
                )
                await self._conn.execute("DELETE FROM cart_items WHERE user_id = ?", (customer_id,))
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise

        order = await self.get_order(order_id)
        assert order is not None
        return order

    async def get_order(self, order_id: int) -> Order | None:
        async with self._lock, self._conn.execute(
            "SELECT o.*, c.username AS username FROM orders o"
            " LEFT JOIN customers c ON c.tg_id = o.customer_id"
            " WHERE o.id = ?",
            (order_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return _order(row, await self._order_lines(order_id))

    async def _order_lines(self, order_id: int) -> list[OrderLine]:
        async with self._lock, self._conn.execute(
            "SELECT * FROM order_items WHERE order_id = ? ORDER BY id", (order_id,)
        ) as cursor:
            return [
                OrderLine(
                    product_id=row["product_id"],
                    title=row["title"],
                    price=int(row["price"]),
                    qty=int(row["qty"]),
                )
                for row in await cursor.fetchall()
            ]

    async def list_orders(
        self,
        *,
        statuses: Sequence[str] | None = None,
        customer_id: int | None = None,
        limit: int = 20,
    ) -> list[Order]:
        sql = "SELECT o.*, c.username AS username FROM orders o"
        sql += " LEFT JOIN customers c ON c.tg_id = o.customer_id"
        conditions: list[str] = []
        params: list = []
        if statuses:
            conditions.append(f"o.status IN ({','.join('?' * len(statuses))})")
            params.extend(statuses)
        if customer_id is not None:
            conditions.append("o.customer_id = ?")
            params.append(customer_id)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY o.created_at DESC LIMIT ?"
        params.append(limit)

        async with self._lock, self._conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [_order(row, await self._order_lines(int(row["id"]))) for row in rows]

    async def set_order_status(
        self, order_id: int, status: str, *, restore_stock: bool = True
    ) -> Order | None:
        """Сменить статус. При отмене остатки возвращаются на склад."""
        async with self._lock:
            async with self._conn.execute(
                "SELECT status FROM orders WHERE id = ?", (order_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if row is None or row["status"] == status:
                return None

            was_cancelled = row["status"] == STATUS_CANCELLED
            await self._conn.execute(
                "UPDATE orders SET status = ? WHERE id = ?", (status, order_id)
            )
            if status == STATUS_CANCELLED and restore_stock and not was_cancelled:
                await self._conn.execute(
                    "UPDATE products SET stock = stock + ("
                    "   SELECT qty FROM order_items"
                    "   WHERE order_items.product_id = products.id AND order_id = ?)"
                    " WHERE stock IS NOT NULL AND id IN ("
                    "   SELECT product_id FROM order_items WHERE order_id = ?)",
                    (order_id, order_id),
                )
            await self._conn.commit()

        return await self.get_order(order_id)

    # ── статистика ────────────────────────────────────────────────────────

    async def stats(self, days: int = 30) -> dict[str, int]:
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        async with self._lock, self._conn.execute(
            "SELECT status, COUNT(*) AS total, SUM(goods_total + delivery_price) AS amount"
            " FROM orders WHERE created_at >= ? GROUP BY status",
            (since,),
        ) as cursor:
            rows = await cursor.fetchall()

        counts = {row["status"]: int(row["total"]) for row in rows}
        # Выручкой считаем только то, что не отменено: иначе отменённые заказы
        # рисуют продавцу несуществующие деньги.
        revenue = sum(
            int(row["amount"] or 0) for row in rows if row["status"] != STATUS_CANCELLED
        )
        return {
            "orders": sum(counts.values()),
            "new": counts.get(STATUS_NEW, 0),
            "cancelled": counts.get(STATUS_CANCELLED, 0),
            "revenue": revenue,
        }

    async def top_products(self, days: int = 30, limit: int = 5) -> list[tuple[str, int]]:
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        async with self._lock, self._conn.execute(
            "SELECT i.title AS title, SUM(i.qty) AS total FROM order_items i"
            " JOIN orders o ON o.id = i.order_id"
            " WHERE o.created_at >= ? AND o.status <> 'cancelled'"
            " GROUP BY i.title ORDER BY total DESC LIMIT ?",
            (since, limit),
        ) as cursor:
            return [(row["title"], int(row["total"])) for row in await cursor.fetchall()]

    async def low_stock(self, threshold: int = 3) -> list[Product]:
        async with self._lock, self._conn.execute(
            _PRODUCT_SELECT + " WHERE p.active = 1 AND p.stock IS NOT NULL AND p.stock <= ?"
            " ORDER BY p.stock, p.title",
            (threshold,),
        ) as cursor:
            return [_product(row) for row in await cursor.fetchall()]


# ──────────────────────────────────────────────────────────────────────────────
# Преобразование строк
# ──────────────────────────────────────────────────────────────────────────────

_PRODUCT_SELECT = """
SELECT p.*, COALESCE(c.title, '') AS category_title
FROM products p
LEFT JOIN categories c ON c.id = p.category_id
"""


def _rulower(value: str | None) -> str:
    return value.lower() if isinstance(value, str) else ""


def _category(row: aiosqlite.Row) -> Category:
    return Category(
        id=int(row["id"]),
        title=row["title"],
        position=int(row["position"]),
        active=bool(row["active"]),
    )


def _product(row: aiosqlite.Row) -> Product:
    return Product(
        id=int(row["id"]),
        title=row["title"],
        price=int(row["price"]),
        category_id=row["category_id"],
        description=row["description"],
        photo_id=row["photo_id"],
        stock=row["stock"],
        active=bool(row["active"]),
        position=int(row["position"]),
        category_title=row["category_title"],
    )


def _order(row: aiosqlite.Row, lines: list[OrderLine]) -> Order:
    return Order(
        id=int(row["id"]),
        customer_id=int(row["customer_id"]),
        lines=lines,
        goods_total=int(row["goods_total"]),
        delivery_price=int(row["delivery_price"]),
        delivery=row["delivery"],
        address=row["address"],
        name=row["name"],
        phone=row["phone"],
        comment=row["comment"],
        status=row["status"],
        created_at=datetime.fromisoformat(row["created_at"]),
        username=row["username"],
    )
