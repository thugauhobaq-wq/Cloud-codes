from __future__ import annotations

from botkit.publish import MODE_BRANCH, MODE_REPO

from botfactory import texts
from botfactory.orders import Order
from botfactory.storage import DONE, FAILED, NEW, Build

ORDER = Order(package="shop", title="Магазин у дома", mode=MODE_REPO, private=True)


def build(**changes) -> Build:
    fields = {
        "id": 7,
        "user_id": 1,
        "package": "shop",
        "title": "Магазин у дома",
        "mode": MODE_REPO,
        "private": True,
        "repo": "seller/shop-bot",
        "status": DONE,
        "url": "https://github.com/seller/shop-bot",
        "error": "",
        "created_at": None,
    }
    return Build(**{**fields, **changes})


def test_card_shows_everything_that_goes_to_github():
    text = texts.card(ORDER, 7)

    assert "Заказ №7" in text
    assert "shop" in text and "Магазин у дома" in text
    assert "shop-bot" in text
    assert "новый репозиторий" in text and "приватный" in text


def test_card_shows_the_chosen_mode():
    assert "ветка" in texts.card(ORDER.toggled_mode(), 7)
    assert "публичный" in texts.card(ORDER.toggled_private(), 7)


def test_user_text_cannot_break_the_markup():
    """Название приходит от человека и может содержать что угодно."""
    text = texts.card(Order(package="shop", title="<b>Ой</b>"), 7)

    assert "<b>Ой</b>" not in text
    assert "&lt;b&gt;Ой&lt;/b&gt;" in text


def test_done_counts_files_in_russian():
    assert "24 файла" in texts.done(build(), "https://github.com/seller/shop-bot", 24)
    assert "1 файл" in texts.done(build(), "https://github.com/seller/shop-bot", 1)


def test_done_without_new_files_does_not_look_like_a_failure():
    text = texts.done(build(), "https://github.com/seller/shop-bot", 0)

    assert "0 файлов" not in text
    assert "с прошлого раза" in text


def test_done_names_the_branch_mode():
    text = texts.done(build(mode=MODE_BRANCH), "https://github.com/seller/shop-bot", 3)

    assert "ветка" in text


def test_builds_list_shows_links_and_errors():
    text = texts.builds([build(), build(id=8, status=FAILED, error="нет прав", url="")])

    assert "https://github.com/seller/shop-bot" in text
    assert "нет прав" in text
    assert "№8" in text


def test_empty_history_suggests_what_to_do():
    assert "/help" in texts.builds([])


def test_waiting_build_is_named():
    assert "ждёт подтверждения" in texts.build_line(build(status=NEW, url=""))
