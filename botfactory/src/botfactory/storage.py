"""История сборок.

Заказ живёт в базе, а не в памяти процесса: между «покажи карточку» и
«собрать» пользователь успевает уйти пить чай, а контейнер — перезапуститься.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from botkit import BaseStorage, from_iso, now_iso

from .orders import Order

#: Жизнь сборки. Из `new` уходят ровно один раз — на этом держится защита
#: от двойного нажатия кнопки «Собрать».
NEW = "new"
BUILDING = "building"
DONE = "done"
FAILED = "failed"

STATUS_TITLES = {
    NEW: "ждёт подтверждения",
    BUILDING: "собирается",
    DONE: "готово",
    FAILED: "ошибка",
}


@dataclass(slots=True)
class Build:
    id: int
    user_id: int
    package: str
    title: str
    mode: str
    private: bool
    repo: str
    status: str
    url: str
    error: str
    created_at: datetime | None

    @property
    def order(self) -> Order:
        return Order(
            package=self.package,
            title=self.title,
            mode=self.mode,
            private=self.private,
            repo=self.repo,
        )


class Storage(BaseStorage):
    schema = """
    CREATE TABLE IF NOT EXISTS builds (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        package     TEXT    NOT NULL,
        title       TEXT    NOT NULL DEFAULT '',
        mode        TEXT    NOT NULL,
        private     INTEGER NOT NULL DEFAULT 1,
        repo        TEXT    NOT NULL DEFAULT '',
        status      TEXT    NOT NULL DEFAULT 'new',
        url         TEXT    NOT NULL DEFAULT '',
        error       TEXT    NOT NULL DEFAULT '',
        created_at  TEXT    NOT NULL,
        finished_at TEXT
    );
    CREATE INDEX IF NOT EXISTS builds_status ON builds (status, id);
    """

    async def add(self, order: Order, user_id: int) -> int:
        return await self.execute(
            "INSERT INTO builds (user_id, package, title, mode, private, repo, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                order.package,
                order.title,
                order.mode,
                int(order.private),
                order.repo,
                now_iso(),
            ),
        )

    async def get(self, build_id: int) -> Build | None:
        row = await self.fetch_one("SELECT * FROM builds WHERE id = ?", (build_id,))
        return _build(row) if row else None

    async def update_order(self, build_id: int, order: Order) -> bool:
        """Поправить заказ кнопками — пока сборка не началась."""
        changed = await self.execute_changes(
            "UPDATE builds SET package = ?, title = ?, mode = ?, private = ?, repo = ?"
            " WHERE id = ? AND status = ?",
            (
                order.package,
                order.title,
                order.mode,
                int(order.private),
                order.repo,
                build_id,
                NEW,
            ),
        )
        return bool(changed)

    async def start(self, build_id: int) -> bool:
        """Перевести в «собирается». `False` — кнопку нажали второй раз.

        Именно этим отличается «взял в работу» от повторного нажатия: два
        параллельных пуша в один репозиторий не нужны никому.
        """
        changed = await self.execute_changes(
            "UPDATE builds SET status = ? WHERE id = ? AND status = ?",
            (BUILDING, build_id, NEW),
        )
        return bool(changed)

    async def finish(self, build_id: int, url: str, repo: str) -> None:
        await self.execute(
            "UPDATE builds SET status = ?, url = ?, repo = ?, error = '', finished_at = ?"
            " WHERE id = ?",
            (DONE, url, repo, now_iso(), build_id),
        )

    async def fail(self, build_id: int, error: str) -> None:
        """Записать ошибку. Причина остаётся в базе — её показывает `/builds`."""
        await self.execute(
            "UPDATE builds SET status = ?, error = ?, finished_at = ? WHERE id = ?",
            (FAILED, error[:500], now_iso(), build_id),
        )

    async def drop(self, build_id: int) -> bool:
        """Отменить заказ. Начатую сборку не отменяем — она уже в GitHub."""
        changed = await self.execute_changes(
            "DELETE FROM builds WHERE id = ? AND status = ?", (build_id, NEW)
        )
        return bool(changed)

    async def retry(self, build_id: int) -> bool:
        changed = await self.execute_changes(
            "UPDATE builds SET status = ?, error = '' WHERE id = ? AND status = ?",
            (NEW, build_id, FAILED),
        )
        return bool(changed)

    async def recent(self, limit: int = 10, *, user_id: int | None = None) -> list[Build]:
        sql = "SELECT * FROM builds"
        params: list = []
        if user_id is not None:
            sql += " WHERE user_id = ?"
            params.append(user_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [_build(row) for row in await self.fetch_all(sql, params)]

    async def unfinished(self) -> list[Build]:
        """Сборки, зависшие в «собирается» после падения процесса."""
        return [
            _build(row)
            for row in await self.fetch_all(
                "SELECT * FROM builds WHERE status = ? ORDER BY id", (BUILDING,)
            )
        ]

    async def stats(self) -> dict[str, int]:
        rows = await self.fetch_all("SELECT status, COUNT(*) AS n FROM builds GROUP BY status")
        counts = {row["status"]: row["n"] for row in rows}
        return {
            "total": sum(counts.values()),
            "done": counts.get(DONE, 0),
            "failed": counts.get(FAILED, 0),
            "waiting": counts.get(NEW, 0) + counts.get(BUILDING, 0),
        }


def _build(row) -> Build:
    return Build(
        id=row["id"],
        user_id=row["user_id"],
        package=row["package"],
        title=row["title"],
        mode=row["mode"],
        private=bool(row["private"]),
        repo=row["repo"],
        status=row["status"],
        url=row["url"],
        error=row["error"],
        created_at=from_iso(row["created_at"]),
    )
