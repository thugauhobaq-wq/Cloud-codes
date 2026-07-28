from __future__ import annotations

from datetime import UTC, datetime

from conftest import cart, product
from shop.config import Settings
from shop.keyboards import page_count
from shop.models import DELIVERY_COURIER, DELIVERY_PICKUP, Cart, Order, OrderLine
from shop.texts import cart_text, order_text, product_card


def test_empty_cart_text(settings: Settings):
    assert "пуста" in cart_text(Cart(), settings)


def test_cart_shows_lines_and_subtotal(settings: Settings):
    text = cart_text(cart((product(price=990, title="Чехол"), 2)), settings)
    assert "Чехол" in text
    assert "2 × 990 ₽" in text
    assert "1 980 ₽" in text


def test_cart_hints_how_much_is_left_for_free_delivery(settings: Settings):
    text = cart_text(cart((product(price=1000), 1)), settings)
    assert "До бесплатной доставки: 2 000 ₽" in text


def test_cart_announces_free_delivery(settings: Settings):
    text = cart_text(cart((product(price=3000), 1)), settings)
    assert "бесплатно" in text


def test_cart_with_delivery_shows_the_total(settings: Settings):
    text = cart_text(cart((product(price=1000), 1)), settings, delivery=DELIVERY_COURIER)
    assert "Доставка: <b>300 ₽</b>" in text
    assert "Итого: <b>1 300 ₽</b>" in text


def test_product_card_reports_low_stock(settings: Settings):
    card = product_card(product(price=990, title="Чехол", stock=2), settings, in_cart=1)
    assert "Осталось: 2 шт." in card
    assert "В корзине: 1 шт." in card


def test_product_card_hides_stock_when_tracking_is_off(settings: Settings):
    settings.track_stock = False
    assert "Осталось" not in product_card(product(stock=2), settings)


def test_order_text_for_admin_includes_contacts(settings: Settings):
    order = Order(
        id=7,
        customer_id=42,
        lines=[OrderLine(product_id=1, title="Чехол", price=990, qty=2)],
        goods_total=1980,
        delivery_price=300,
        delivery=DELIVERY_COURIER,
        address="Ленина, 1",
        name="Иван",
        phone="+7 900 000-00-00",
        created_at=datetime(2026, 7, 28, 12, tzinfo=UTC),
        username="ivan",
    )

    text = order_text(order, settings, for_admin=True)
    assert "Заказ №7" in text
    assert "Итого: 2 280 ₽" in text
    assert "+7 900 000-00-00" in text
    assert "@ivan" in text

    # Покупателю телефон и username показывать незачем — он их и так знает.
    assert "@ivan" not in order_text(order, settings)


def test_pickup_order_has_no_delivery_line(settings: Settings):
    order = Order(
        id=8,
        customer_id=42,
        lines=[OrderLine(product_id=1, title="Чехол", price=990, qty=1)],
        goods_total=990,
        delivery=DELIVERY_PICKUP,
        name="Иван",
        phone="+7 900",
    )
    assert "Доставка" not in order_text(order, settings)


def test_html_in_product_titles_is_escaped(settings: Settings):
    card = product_card(product(title="<b>Чехол</b>"), settings)
    assert "&lt;b&gt;" in card


def test_page_count():
    assert page_count(0, 6) == 0
    assert page_count(6, 6) == 1
    assert page_count(7, 6) == 2
    assert page_count(13, 6) == 3
