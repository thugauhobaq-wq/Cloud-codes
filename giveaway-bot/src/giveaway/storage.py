"""Хранилище: розыгрыши, участники, победители.

Схема описывается строкой, всё остальное — открытие базы, блокировки,
транзакции — берётся из botkit.BaseStorage.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import aiosqlite
from botkit import BaseStorage, from_iso, now_iso, to_iso

from .config import parse_channels
from .draw import make_seed, pick_winners, reroll_seed
from .models import (
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_FINISHED,
    Giveaway,
    Participant,
    Winner,
)


class AlreadyFinished(RuntimeError):
    """Итоги уже подведены. Второй раз разыгрывать нечего."""


class NoParticipants(RuntimeError):
    """Разыгрывать не между кем."""


class Storage(BaseStorage):
    schema = """
    CREATE TABLE IF NOT EXISTS giveaways (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        title         TEXT NOT NULL,
        prize         TEXT NOT NULL DEFAULT '',
        winners_count INTEGER NOT NULL DEFAULT 1,
        status        TEXT NOT NULL DEFAULT 'active',
        channels      TEXT NOT NULL DEFAULT '',
        ends_at       TEXT,
        created_at    TEXT NOT NULL,
        finished_at   TEXT,
        -- Зерно жребия: появляется при завершении и публикуется с итогами.
        seed          TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS giveaways_status_idx ON giveaways (status, ends_at);

    CREATE TABLE IF NOT EXISTS participants (
        giveaway_id INTEGER NOT NULL REFERENCES giveaways (id) ON DELETE CASCADE,
        tg_id       INTEGER NOT NULL,
        name        TEXT NOT NULL DEFAULT '',
        username    TEXT,
        joined_at   TEXT NOT NULL,
        -- Один человек — одно участие: первичный ключ, а не проверка в коде,
        -- потому что кнопку «Участвовать» жмут по нескольку раз подряд.
        PRIMARY KEY (giveaway_id, tg_id)
    );

    CREATE TABLE IF NOT EXISTS winners (
        giveaway_id  INTEGER NOT NULL REFERENCES giveaways (id) ON DELETE CASCADE,
        tg_id        INTEGER NOT NULL,
        place        INTEGER NOT NULL,
        seed         TEXT NOT NULL DEFAULT '',
        round_number INTEGER NOT NULL DEFAULT 0,
        notified     INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (giveaway_id, place)
    );
    """

    # ── розыгрыши ─────────────────────────────────────────────────────────

    async def create_giveaway(
        self,
        title: str,
        *,
        prize: str = "",
        winners_count: int = 1,
        ends_at: datetime | None = None,
        channels: Sequence[str] = (),
    ) -> Giveaway:
        giveaway_id = await self.execute(
            "INSERT INTO giveaways (title, prize, winners_count, channels, ends_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                title,
                prize,
                max(1, winners_count),
                ",".join(channels),
                to_iso(ends_at) if ends_at else None,
                now_iso(),
            ),
        )
        giveaway = await self.get_giveaway(giveaway_id)
        assert giveaway is not None  # вставили строкой выше
        return giveaway

    async def get_giveaway(self, giveaway_id: int) -> Giveaway | None:
        row = await self.fetch_one(_GIVEAWAY_SELECT + " WHERE g.id = ?", (giveaway_id,))
        return _giveaway(row) if row else None

    async def list_giveaways(
        self, *, statuses: Sequence[str] | None = None, limit: int = 20
    ) -> list[Giveaway]:
        sql = _GIVEAWAY_SELECT
        params: list = []
        if statuses:
            sql += f" WHERE g.status IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
        sql += " ORDER BY g.created_at DESC LIMIT ?"
        params.append(limit)
        return [_giveaway(row) for row in await self.fetch_all(sql, params)]

    async def due_giveaways(self, now: datetime | None = None) -> list[Giveaway]:
        """Активные розыгрыши, у которых вышел срок."""
        rows = await self.fetch_all(
            _GIVEAWAY_SELECT + " WHERE g.status = ? AND g.ends_at IS NOT NULL AND g.ends_at <= ?"
            " ORDER BY g.ends_at",
            (STATUS_ACTIVE, to_iso(now or datetime.now(UTC))),
        )
        return [_giveaway(row) for row in rows]

    async def cancel_giveaway(self, giveaway_id: int) -> Giveaway | None:
        """Отменить. `None` — отменять было нечего: уже завершён или отменён."""
        changed = await self.execute_changes(
            "UPDATE giveaways SET status = ? WHERE id = ? AND status = ?",
            (STATUS_CANCELLED, giveaway_id, STATUS_ACTIVE),
        )
        return await self.get_giveaway(giveaway_id) if changed else None

    # ── участники ─────────────────────────────────────────────────────────

    async def join(
        self, giveaway_id: int, tg_id: int, *, name: str = "", username: str | None = None
    ) -> tuple[Participant, bool]:
        """Записать участника. Второй элемент — участвовал ли он уже раньше.

        Повторное нажатие кнопки не должно выглядеть ошибкой: человек просто
        хочет убедиться, что он в списке.

        В завершённый розыгрыш записать нельзя: зерно посчитано по составу
        участников, и любой опоздавший ломает проверку итогов. Обработчик это
        проверяет заранее, но между его проверкой и записью проходит время —
        а условие в самом INSERT проверяется в тот же момент, что и вставка.
        """
        existing = await self.get_participant(giveaway_id, tg_id)
        if existing is not None:
            return existing, True

        await self.execute(
            "INSERT INTO participants (giveaway_id, tg_id, name, username, joined_at)"
            " SELECT ?, ?, ?, ?, ? FROM giveaways WHERE id = ? AND status = ?"
            " ON CONFLICT (giveaway_id, tg_id) DO NOTHING",
            (giveaway_id, tg_id, name, username, now_iso(), giveaway_id, STATUS_ACTIVE),
        )
        participant = await self.get_participant(giveaway_id, tg_id)
        if participant is None:
            raise AlreadyFinished(str(giveaway_id))
        return participant, False

    async def get_participant(self, giveaway_id: int, tg_id: int) -> Participant | None:
        row = await self.fetch_one(
            "SELECT p.*, ("
            "   SELECT COUNT(*) FROM participants earlier"
            "   WHERE earlier.giveaway_id = p.giveaway_id AND earlier.joined_at <= p.joined_at"
            " ) AS number"
            " FROM participants p WHERE p.giveaway_id = ? AND p.tg_id = ?",
            (giveaway_id, tg_id),
        )
        return _participant(row) if row else None

    async def participant_ids(self, giveaway_id: int) -> list[int]:
        rows = await self.fetch_all(
            "SELECT tg_id FROM participants WHERE giveaway_id = ? ORDER BY tg_id",
            (giveaway_id,),
        )
        return [int(row["tg_id"]) for row in rows]

    async def count_participants(self, giveaway_id: int) -> int:
        return int(
            await self.fetch_value(
                "SELECT COUNT(*) FROM participants WHERE giveaway_id = ?", (giveaway_id,), 0
            )
        )

    async def last_participants(self, giveaway_id: int, limit: int = 10) -> list[Participant]:
        rows = await self.fetch_all(
            "SELECT *, 0 AS number FROM participants WHERE giveaway_id = ?"
            " ORDER BY joined_at DESC LIMIT ?",
            (giveaway_id, limit),
        )
        return [_participant(row) for row in rows]

    async def giveaways_of(self, tg_id: int, limit: int = 10) -> list[Giveaway]:
        rows = await self.fetch_all(
            _GIVEAWAY_SELECT + " JOIN participants p2 ON p2.giveaway_id = g.id"
            " WHERE p2.tg_id = ? ORDER BY g.created_at DESC LIMIT ?",
            (tg_id, limit),
        )
        return [_giveaway(row) for row in rows]

    async def export_rows(self, giveaway_id: int) -> list[tuple]:
        rows = await self.fetch_all(
            "SELECT p.tg_id, p.name, p.username, p.joined_at,"
            "       CASE WHEN w.tg_id IS NULL THEN '' ELSE w.place END AS place"
            " FROM participants p"
            " LEFT JOIN winners w ON w.giveaway_id = p.giveaway_id AND w.tg_id = p.tg_id"
            " WHERE p.giveaway_id = ? ORDER BY p.joined_at",
            (giveaway_id,),
        )
        return [tuple(row) for row in rows]

    # ── итоги ─────────────────────────────────────────────────────────────

    async def finish(self, giveaway_id: int, now: datetime | None = None) -> list[Winner]:
        """Подвести итоги: зафиксировать зерно и записать победителей.

        Всё одной транзакцией: между «выбрали» и «записали» никто не должен
        успеть присоединиться, иначе объявленное зерно не сойдётся с составом
        участников, и проверка перестанет работать.
        """
        now = now or datetime.now(UTC)
        async with self.transaction() as conn:
            async with conn.execute(
                "SELECT status, winners_count FROM giveaways WHERE id = ?", (giveaway_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if row is None or row["status"] != STATUS_ACTIVE:
                raise AlreadyFinished(str(giveaway_id))

            async with conn.execute(
                "SELECT tg_id FROM participants WHERE giveaway_id = ? ORDER BY tg_id",
                (giveaway_id,),
            ) as cursor:
                participants = [int(item["tg_id"]) for item in await cursor.fetchall()]
            if not participants:
                raise NoParticipants(str(giveaway_id))

            seed = make_seed(giveaway_id, now, participants)
            winners = pick_winners(participants, int(row["winners_count"]), seed)

            await conn.execute(
                "UPDATE giveaways SET status = ?, finished_at = ?, seed = ? WHERE id = ?",
                (STATUS_FINISHED, to_iso(now), seed, giveaway_id),
            )
            await conn.executemany(
                "INSERT INTO winners (giveaway_id, tg_id, place, seed) VALUES (?, ?, ?, ?)",
                [(giveaway_id, tg_id, place, seed) for place, tg_id in enumerate(winners, 1)],
            )

        return await self.winners(giveaway_id)

    async def winners(self, giveaway_id: int) -> list[Winner]:
        rows = await self.fetch_all(
            "SELECT w.*, COALESCE(p.name, '') AS name, p.username AS username"
            " FROM winners w"
            " LEFT JOIN participants p"
            "   ON p.giveaway_id = w.giveaway_id AND p.tg_id = w.tg_id"
            " WHERE w.giveaway_id = ? ORDER BY w.place",
            (giveaway_id,),
        )
        return [_winner(row) for row in rows]

    async def reroll(self, giveaway_id: int, place: int) -> Winner | None:
        """Заменить победителя, который не откликнулся.

        Новое зерно получается из прежнего и номера попытки, поэтому замена
        проверяется так же, как основной жребий, а организатор не может
        крутить её, пока не выпадет нужный человек. Уже выигравшие в новый
        розыгрыш не попадают.
        """
        giveaway = await self.get_giveaway(giveaway_id)
        if giveaway is None or giveaway.status != STATUS_FINISHED:
            return None

        current = await self.fetch_one(
            "SELECT * FROM winners WHERE giveaway_id = ? AND place = ?", (giveaway_id, place)
        )
        if current is None:
            return None

        taken = {int(item.tg_id) for item in await self.winners(giveaway_id)}
        pool = [item for item in await self.participant_ids(giveaway_id) if item not in taken]
        if not pool:
            return None

        round_number = int(current["round_number"]) + 1
        seed = reroll_seed(giveaway.seed or "", round_number)
        replacement = pick_winners(pool, 1, seed)[0]

        await self.execute(
            "UPDATE winners SET tg_id = ?, seed = ?, round_number = ?, notified = 0"
            " WHERE giveaway_id = ? AND place = ?",
            (replacement, seed, round_number, giveaway_id, place),
        )
        return next((item for item in await self.winners(giveaway_id) if item.place == place), None)

    async def mark_notified(self, giveaway_id: int, tg_id: int) -> None:
        await self.execute(
            "UPDATE winners SET notified = 1 WHERE giveaway_id = ? AND tg_id = ?",
            (giveaway_id, tg_id),
        )

    async def stats(self) -> dict[str, int]:
        return {
            "giveaways": int(await self.fetch_value("SELECT COUNT(*) FROM giveaways", (), 0)),
            "active": int(
                await self.fetch_value(
                    "SELECT COUNT(*) FROM giveaways WHERE status = ?", (STATUS_ACTIVE,), 0
                )
            ),
            "participants": int(await self.fetch_value("SELECT COUNT(*) FROM participants", (), 0)),
            "people": int(
                await self.fetch_value("SELECT COUNT(DISTINCT tg_id) FROM participants", (), 0)
            ),
        }


# ──────────────────────────────────────────────────────────────────────────────
# Преобразование строк
# ──────────────────────────────────────────────────────────────────────────────

_GIVEAWAY_SELECT = """
SELECT g.*, (
    SELECT COUNT(*) FROM participants p WHERE p.giveaway_id = g.id
) AS participants
FROM giveaways g
"""


def _giveaway(row: aiosqlite.Row) -> Giveaway:
    return Giveaway(
        id=int(row["id"]),
        title=row["title"],
        prize=row["prize"],
        winners_count=int(row["winners_count"]),
        status=row["status"],
        ends_at=from_iso(row["ends_at"]),
        created_at=from_iso(row["created_at"]),
        finished_at=from_iso(row["finished_at"]),
        channels=parse_channels(row["channels"]),
        seed=row["seed"],
        participants=int(row["participants"]),
    )


def _participant(row: aiosqlite.Row) -> Participant:
    return Participant(
        giveaway_id=int(row["giveaway_id"]),
        tg_id=int(row["tg_id"]),
        name=row["name"],
        username=row["username"],
        number=int(row["number"] or 0),
        joined_at=from_iso(row["joined_at"]),
    )


def _winner(row: aiosqlite.Row) -> Winner:
    return Winner(
        giveaway_id=int(row["giveaway_id"]),
        tg_id=int(row["tg_id"]),
        place=int(row["place"]),
        seed=row["seed"],
        round_number=int(row["round_number"]),
        notified=bool(row["notified"]),
        name=row["name"],
        username=row["username"],
    )
