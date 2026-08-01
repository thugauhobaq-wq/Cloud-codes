"""Состояние: дедупликация, настройки, статистика."""

from __future__ import annotations

from conftest import make_order
from fparser.storage import Filters, Storage


async def test_new_orders_are_new_only_once(storage: Storage):
    orders = [make_order(native_id="1"), make_order(native_id="2")]

    assert len(await storage.select_new(orders)) == 2

    await storage.mark_seen(orders, sent=True)

    assert await storage.select_new(orders) == []


async def test_duplicates_inside_one_batch_are_collapsed(storage: Storage):
    """Один и тот же заказ приходит в ленте дважды чаще, чем хотелось бы."""
    orders = [make_order(native_id="7"), make_order(native_id="7")]

    assert len(await storage.select_new(orders)) == 1


async def test_same_id_on_different_boards_is_not_a_duplicate(storage: Storage):
    """Заказ №1 есть на каждой бирже — ключ обязан включать площадку."""
    orders = [
        make_order(source="kwork", native_id="1"),
        make_order(source="flru", native_id="1"),
    ]

    assert len(await storage.select_new(orders)) == 2


async def test_filters_survive_restart(storage: Storage, tmp_path):
    filters = Filters(
        keywords=["питон", "бот"],
        stopwords=["логотип"],
        min_budget=5000,
        sources=["kwork"],
        categories=["Разработка сайтов"],
        paused=True,
    )
    await storage.save_filters(filters)
    await storage.close()

    # Ради этого всё и затевалось: перезапуск контейнера не должен сбрасывать
    # настройки и заново высылать всю ленту.
    reopened = Storage(str(tmp_path / "test.db"))
    await reopened.open()
    try:
        restored = await reopened.get_filters()
    finally:
        await reopened.close()

    assert restored == filters


async def test_defaults_when_nothing_saved(storage: Storage):
    filters = await storage.get_filters()

    assert filters.keywords == []
    assert filters.min_budget == 0
    assert set(filters.sources) == {"kwork", "flru", "habr", "weblancer"}
    assert filters.paused is False


async def test_corrupted_setting_falls_back_to_default(storage: Storage):
    await storage._conn.execute(
        "INSERT INTO settings (key, value) VALUES ('filter.keywords', 'не json')"
    )
    await storage._conn.commit()

    filters = await storage.get_filters()

    assert filters.keywords == []


async def test_sent_flag_is_recorded(storage: Storage):
    sent = make_order(native_id="1", title="Отправленный")
    skipped = make_order(native_id="2", title="Отсеянный")

    await storage.mark_seen([sent], sent=True)
    await storage.mark_seen([skipped], sent=False)

    assert [row["title"] for row in await storage.last_sent()] == ["Отправленный"]


async def test_stats_accumulate_within_a_day(storage: Storage):
    await storage.record_stats("kwork", found=10, sent=2)
    await storage.record_stats("kwork", found=5, sent=1)

    rows = await storage.stats_since(7)

    assert len(rows) == 1
    assert (rows[0]["found"], rows[0]["sent"]) == (15, 3)


async def test_categories_are_learned_from_orders(storage: Storage):
    await storage.note_categories(
        [
            make_order(native_id="1", category="Разработка сайтов"),
            make_order(native_id="2", category="Разработка сайтов"),
            make_order(native_id="3", category=None),
        ]
    )

    rows = await storage.known_categories()

    assert [row["category"] for row in rows] == ["Разработка сайтов"]
    assert rows[0]["id"] is not None


async def test_category_id_is_stable_across_updates(storage: Storage):
    """Кнопки `/categories` ссылаются на id, поэтому он не должен меняться
    при повторной встрече рубрики — иначе кнопка переключит не то."""
    await storage.note_categories([make_order(category="Дизайн")])
    first = (await storage.known_categories())[0]["id"]

    await storage.note_categories([make_order(native_id="2", category="Дизайн")])
    second = (await storage.known_categories())[0]["id"]

    assert first == second
    assert await storage.category_by_id(first) is not None


async def test_favorites_roundtrip(storage: Storage):
    await storage.add_favorite("kwork:1", "Заголовок", "https://kwork.ru/projects/1")
    await storage.add_favorite("kwork:1", "Заголовок", "https://kwork.ru/projects/1")

    rows = await storage.list_favorites()

    assert len(rows) == 1
    assert rows[0]["title"] == "Заголовок"


async def test_prune_removes_only_old_rows(storage: Storage):
    await storage.mark_seen([make_order(native_id="1")], sent=True)

    assert await storage.prune(days=30) == 0
    assert await storage.count_orders() == 1

    assert await storage.prune(days=0) == 1
    assert await storage.count_orders() == 0


async def test_state_roundtrip(storage: Storage):
    assert await storage.get_state("initialized") is None

    await storage.set_state("initialized", "2026-07-28T04:00:00+00:00")

    assert await storage.get_state("initialized") == "2026-07-28T04:00:00+00:00"


async def test_order_lookup_by_uid(storage: Storage):
    await storage.mark_seen([make_order(native_id="55", title="Искомый")], sent=True)

    row = await storage.order_by_uid("kwork:55")

    assert row is not None
    assert row["title"] == "Искомый"
    assert await storage.order_by_uid("kwork:404") is None
