from __future__ import annotations

from botkit.publish import MODE_BRANCH, MODE_REPO

from botfactory.orders import Order
from botfactory.storage import BUILDING, DONE, FAILED, NEW, Storage

ORDER = Order(package="shop", title="Магазин у дома", mode=MODE_REPO, private=True)


async def test_order_survives_a_restart(storage: Storage):
    """Карточку нажимают через час — заказ должен лежать в базе, а не в памяти."""
    build_id = await storage.add(ORDER, user_id=7)

    build = await storage.get(build_id)

    assert build is not None
    assert build.order == ORDER
    assert build.status == NEW
    assert build.user_id == 7


async def test_second_press_does_not_start_a_second_build(storage: Storage):
    build_id = await storage.add(ORDER, user_id=7)

    assert await storage.start(build_id) is True
    # Кнопку нажали дважды: второй пуш в тот же репозиторий не нужен никому.
    assert await storage.start(build_id) is False


async def test_finished_build_keeps_the_link(storage: Storage):
    build_id = await storage.add(ORDER, user_id=7)
    await storage.start(build_id)

    await storage.finish(build_id, "https://github.com/seller/shop-bot", "seller/shop-bot")

    build = await storage.get(build_id)
    assert build is not None
    assert build.status == DONE
    assert build.url == "https://github.com/seller/shop-bot"


async def test_failed_build_can_be_repeated(storage: Storage):
    build_id = await storage.add(ORDER, user_id=7)
    await storage.start(build_id)
    await storage.fail(build_id, "GitHub не принял токен")

    assert await storage.retry(build_id) is True

    build = await storage.get(build_id)
    assert build is not None and build.status == NEW and build.error == ""


async def test_successful_build_is_not_repeated(storage: Storage):
    build_id = await storage.add(ORDER, user_id=7)
    await storage.start(build_id)
    await storage.finish(build_id, "https://github.com/seller/shop-bot", "seller/shop-bot")

    assert await storage.retry(build_id) is False


async def test_order_is_edited_by_buttons_until_the_build_starts(storage: Storage):
    build_id = await storage.add(ORDER, user_id=7)

    assert await storage.update_order(build_id, ORDER.toggled_mode()) is True
    build = await storage.get(build_id)
    assert build is not None and build.mode == MODE_BRANCH

    await storage.start(build_id)
    # Сборка пошла — менять заказ поздно, репозиторий уже создаётся.
    assert await storage.update_order(build_id, ORDER.toggled_private()) is False


async def test_started_build_cannot_be_cancelled(storage: Storage):
    build_id = await storage.add(ORDER, user_id=7)

    await storage.start(build_id)

    assert await storage.drop(build_id) is False
    assert await storage.get(build_id) is not None


async def test_cancelled_order_disappears(storage: Storage):
    build_id = await storage.add(ORDER, user_id=7)

    assert await storage.drop(build_id) is True
    assert await storage.get(build_id) is None


async def test_long_error_is_trimmed_and_kept(storage: Storage):
    build_id = await storage.add(ORDER, user_id=7)
    await storage.start(build_id)

    await storage.fail(build_id, "ы" * 900)

    build = await storage.get(build_id)
    assert build is not None and build.status == FAILED and len(build.error) == 500


async def test_unfinished_builds_are_visible_after_a_crash(storage: Storage):
    first = await storage.add(ORDER, user_id=7)
    second = await storage.add(ORDER, user_id=7)
    await storage.start(first)

    unfinished = await storage.unfinished()

    assert [build.id for build in unfinished] == [first]
    assert (await storage.get(second)).status == NEW  # type: ignore[union-attr]


async def test_recent_is_newest_first_and_can_be_filtered(storage: Storage):
    mine = await storage.add(ORDER, user_id=7)
    other = await storage.add(ORDER, user_id=8)

    assert [build.id for build in await storage.recent(10)] == [other, mine]
    assert [build.id for build in await storage.recent(10, user_id=7)] == [mine]


async def test_stats_count_by_status(storage: Storage):
    done = await storage.add(ORDER, user_id=7)
    await storage.start(done)
    await storage.finish(done, "https://github.com/seller/shop-bot", "seller/shop-bot")
    broken = await storage.add(ORDER, user_id=7)
    await storage.start(broken)
    await storage.fail(broken, "нет прав")
    await storage.add(ORDER, user_id=7)

    assert await storage.stats() == {"total": 3, "done": 1, "failed": 1, "waiting": 1}


async def test_building_status_is_named_for_people():
    from botfactory.storage import STATUS_TITLES

    assert STATUS_TITLES[BUILDING] == "собирается"
