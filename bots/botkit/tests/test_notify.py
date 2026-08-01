from __future__ import annotations

from botkit import Notifier
from botkit.testing import FakeBot


async def test_all_admins_are_notified():
    bot = FakeBot()
    delivered = await Notifier(bot, [1, 2]).notify_admins("Новый заказ")

    assert delivered == 2
    assert bot.to(1) == ["Новый заказ"]
    assert bot.to(2) == ["Новый заказ"]


async def test_one_unreachable_admin_does_not_block_the_others():
    """Админ, заблокировавший бота, не должен мешать остальным узнать о заказе."""
    bot = FakeBot(unreachable={1})
    delivered = await Notifier(bot, [1, 2]).notify_admins("Новый заказ")

    assert delivered == 1
    assert bot.to(2) == ["Новый заказ"]


async def test_unreachable_user_is_reported_not_raised():
    bot = FakeBot(unreachable={42})
    assert await Notifier(bot, [1]).notify(42, "Заказ принят") is False


async def test_duplicate_admin_ids_are_collapsed():
    bot = FakeBot()
    notifier = Notifier(bot, [1, 1, 2])

    assert notifier.admins == [1, 2]
    await notifier.notify_admins("Раз")
    assert bot.to(1) == ["Раз"]
