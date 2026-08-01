"""Сущности воронки: лид-магнит, подписчик, выдача, рассылка."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

KIND_TEXT = "text"
KIND_LINK = "link"
KIND_FILE = "file"
KIND_PHOTO = "photo"

KIND_TITLES = {
    KIND_TEXT: "текст",
    KIND_LINK: "ссылка",
    KIND_FILE: "файл",
    KIND_PHOTO: "картинка",
}


@dataclass(slots=True)
class Magnet:
    """Лид-магнит: что выдаём и по какому кодовому слову."""

    id: int
    code: str
    title: str
    kind: str = KIND_TEXT
    body: str = ""
    file_id: str = ""
    followup: str = ""
    active: bool = True
    created_at: datetime | None = None

    @property
    def ready(self) -> bool:
        """Есть ли что выдавать.

        Магнит заводят в два шага — сначала кодовое слово и описание, потом
        файл, — и между ними он выдавать нечего.
        """
        if self.kind in {KIND_FILE, KIND_PHOTO}:
            return bool(self.file_id)
        return bool(self.body)


@dataclass(slots=True)
class Lead:
    """Подписчик. `source` — метка из deep-link, чтобы видеть, что сработало."""

    tg_id: int
    name: str = ""
    username: str | None = None
    source: str = ""
    created_at: datetime | None = None
    blocked: bool = False


@dataclass(slots=True)
class Delivery:
    """Факт выдачи магнита конкретному человеку."""

    id: int
    lead_id: int
    magnet_id: int
    created_at: datetime
    followup_sent_at: datetime | None = None
    source: str = ""
    magnet_code: str = ""
    magnet_title: str = ""


@dataclass(slots=True)
class Broadcast:
    id: int
    text: str
    magnet_code: str = ""
    total: int = 0
    sent: int = 0
    failed: int = 0
    status: str = "pending"
    created_at: datetime | None = None
