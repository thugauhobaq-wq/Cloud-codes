"""Фоновая часть: подведение итогов по сроку.

Очередь — сами розыгрыши в базе, а не таймеры в памяти: перезапуск контейнера
не должен приводить к тому, что розыгрыш висит завершённым по времени, но без
объявленных победителей.
"""

from __future__ import annotations

import logging

from botkit import PeriodicWorker

from .config import Settings
from .notify import Notify
from .storage import AlreadyFinished, NoParticipants, Storage

log = logging.getLogger(__name__)


class Deadlines(PeriodicWorker):
    name = "итоги"

    def __init__(self, storage: Storage, notify: Notify, settings: Settings) -> None:
        super().__init__(settings.deadline_interval_sec)
        self._storage = storage
        self._notify = notify
        self._settings = settings

    async def tick(self) -> int:
        finished = 0
        for giveaway in await self._storage.due_giveaways():
            try:
                winners = await self._storage.finish(giveaway.id)
            except NoParticipants:
                # Разыгрывать не с кем. Закрываем, чтобы не проверять его
                # каждую минуту, и говорим владельцу — это его новость.
                await self._storage.cancel_giveaway(giveaway.id)
                await self._notify.notify_admins(
                    f"🎁 Розыгрыш «{giveaway.title}» закончился без участников."
                )
                continue
            except AlreadyFinished:
                # Кто-то подвёл итоги вручную за секунду до нас.
                continue

            fresh = await self._storage.get_giveaway(giveaway.id) or giveaway
            await self._notify.announce(fresh, winners)
            unreachable = await self._notify.tell_winners(fresh, winners)
            for winner in winners:
                if winner not in unreachable:
                    await self._storage.mark_notified(fresh.id, winner.tg_id)

            if unreachable:
                await self._notify.notify_admins(
                    f"⚠️ Розыгрыш «{fresh.title}»: не удалось написать "
                    f"{len(unreachable)} победителям — они заблокировали бота. "
                    "Их можно перевыбрать: /winners " + str(fresh.id)
                )
            finished += 1
        return finished
