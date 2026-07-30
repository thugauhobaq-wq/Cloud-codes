"""Хранение предложений в JSON.

Одно предложение — один файл: его можно положить в git, отправить коллеге или
открыть руками. Логотипы лежат внутри того же файла в base64, чтобы черновик не
рассыпался при переносе, а рядом с документом — состояние доставки: кому ушло,
когда открыли, чем кончилось.

Отдельной базы нет намеренно. У небольшой студии предложений десятки, а не
миллионы; перебор файлов дешевле, чем схема, миграции и ещё один формат данных
в проекте, где всё остальное — обычный текст.
"""

from __future__ import annotations

import base64
import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .delivery import Delivery
from .models import Proposal

DEFAULT_DIR = Path.home() / ".proposals"
SUFFIX = ".proposal.json"


class StorageError(RuntimeError):
    """Черновик не читается или не пишется."""


@dataclass(slots=True)
class Record:
    """Файл черновика: сам документ плюс его доставка."""

    proposal: Proposal
    delivery: Delivery = field(default_factory=Delivery)


def to_json(record: Record | Proposal) -> dict:
    """Запись → словарь для файла, вместе с логотипами."""
    if isinstance(record, Proposal):  # для короткого вызова из CLI и тестов
        record = Record(proposal=record)
    proposal = record.proposal
    raw = proposal.to_dict()
    if proposal.sender.logo:
        raw["sender"]["logo"] = base64.b64encode(proposal.sender.logo).decode("ascii")
    if proposal.client.logo:
        raw["client"]["logo"] = base64.b64encode(proposal.client.logo).decode("ascii")
    raw["delivery"] = record.delivery.to_dict()
    return raw


def from_json(raw: dict) -> Record:
    """Словарь → запись. Логотипы принимаем и как base64, и как data-URL."""
    proposal = Proposal.from_dict(raw)
    proposal.sender.logo = _decode_logo((raw.get("sender") or {}).get("logo"))
    proposal.client.logo = _decode_logo((raw.get("client") or {}).get("logo"))
    return Record(proposal=proposal, delivery=Delivery.from_dict(raw.get("delivery")))


def _decode_logo(value: object) -> bytes | None:
    if not value:
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    text = str(value).strip()
    if text.startswith("data:"):
        _, _, text = text.partition(",")
    try:
        return base64.b64decode(text, validate=False)
    except (ValueError, TypeError):
        return None


def load(path: str | Path) -> Record:
    file = Path(path).expanduser()
    try:
        raw = json.loads(file.read_text("utf-8"))
    except OSError as error:
        raise StorageError(f"не читается {file}: {error}") from error
    except json.JSONDecodeError as error:
        raise StorageError(f"{file}: это не JSON — {error}") from error
    if not isinstance(raw, dict):
        raise StorageError(f"{file}: ожидался объект JSON с полями предложения")
    return from_json(raw)


def save(record: Record | Proposal, path: str | Path) -> Path:
    file = Path(path).expanduser()
    file.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(to_json(record), ensure_ascii=False, indent=2) + "\n"
    # Пишем через временный файл: заказчик может открыть ссылку ровно в тот
    # момент, когда мы сохраняем правку, и получить обрезанный файл.
    temporary = file.with_name(f".{file.name}.tmp{os.getpid()}")
    try:
        temporary.write_text(body, "utf-8")
        os.replace(temporary, file)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise StorageError(f"не записывается {file}: {error}") from error
    return file


@dataclass(slots=True)
class DraftInfo:
    """Строка списка черновиков."""

    slug: str
    path: Path
    number: str
    client: str
    subject: str
    updated_at: datetime
    status: str
    status_label: str
    total: str
    """Сумма к оплате, уже отформатированная, — чтобы список не считал сам."""
    currency: str
    sent_at: str
    viewed_at: str
    decided_at: str

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "number": self.number,
            "client": self.client,
            "subject": self.subject,
            "updated_at": self.updated_at.isoformat(timespec="seconds"),
            "status": self.status,
            "status_label": self.status_label,
            "total": self.total,
            "currency": self.currency,
            "sent_at": self.sent_at,
            "viewed_at": self.viewed_at,
            "decided_at": self.decided_at,
        }


class Drafts:
    """Каталог с предложениями — то, чем пользуется веб-форма."""

    def __init__(self, directory: str | Path = DEFAULT_DIR) -> None:
        self.directory = Path(directory).expanduser()
        # Публичную ссылку заказчик может открыть одновременно с правкой в
        # форме; сервер многопоточный, поэтому изменения сериализуем.
        self._lock = threading.Lock()

    def path_for(self, slug: str) -> Path:
        safe = _safe_slug(slug)
        if not safe:
            raise StorageError("пустое имя черновика")
        return self.directory / f"{safe}{SUFFIX}"

    def list(self) -> list[DraftInfo]:
        from .money import format_amount  # локально: storage не тянет вёрстку

        if not self.directory.is_dir():
            return []
        found: list[DraftInfo] = []
        for file in sorted(self.directory.glob(f"*{SUFFIX}")):
            try:
                record = load(file)
            except StorageError:
                continue  # чужой файл в каталоге — не повод падать
            proposal, delivery = record.proposal, record.delivery
            found.append(
                DraftInfo(
                    slug=file.name[: -len(SUFFIX)],
                    path=file,
                    number=proposal.number,
                    client=proposal.client.name,
                    subject=proposal.subject,
                    updated_at=datetime.fromtimestamp(file.stat().st_mtime),
                    status=delivery.status,
                    status_label=delivery.label,
                    total=format_amount(proposal.totals().total, proposal.currency),
                    currency=proposal.currency,
                    sent_at=delivery.sent_at.isoformat() if delivery.sent_at else "",
                    viewed_at=delivery.viewed_at.isoformat() if delivery.viewed_at else "",
                    decided_at=delivery.decided_at.isoformat() if delivery.decided_at else "",
                )
            )
        found.sort(key=lambda info: info.updated_at, reverse=True)
        return found

    def read(self, slug: str) -> Record:
        return load(self.path_for(slug))

    def write(self, slug: str, record: Record | Proposal) -> Path:
        with self._lock:
            return save(record, self.path_for(slug))

    def update(self, slug: str, change) -> Record:
        """Прочитать, изменить и записать под замком.

        Так «просмотрено» от заказчика не затрёт правку, сохранённую из формы
        в ту же секунду.
        """
        with self._lock:
            record = load(self.path_for(slug))
            change(record)
            save(record, self.path_for(slug))
            return record

    def delete(self, slug: str) -> bool:
        with self._lock:
            path = self.path_for(slug)
            if path.is_file():
                path.unlink()
                return True
            return False

    def find_by_token(self, token: str) -> tuple[str, Record] | None:
        """Найти предложение по секрету из публичной ссылки."""
        if not token or not self.directory.is_dir():
            return None
        for file in sorted(self.directory.glob(f"*{SUFFIX}")):
            try:
                record = load(file)
            except StorageError:
                continue
            if record.delivery.matches(token):
                return file.name[: -len(SUFFIX)], record
        return None

    def next_number(self) -> str:
        """Следующий номер КП: максимум из сохранённых плюс один."""
        highest = 0
        for info in self.list():
            digits = "".join(char for char in info.number if char.isdigit())
            if digits:
                highest = max(highest, int(digits))
        return str(highest + 1)


def _safe_slug(slug: str) -> str:
    """Только то, из чего нельзя собрать путь наружу каталога."""
    return "".join(
        char for char in str(slug).strip() if char.isalnum() or char in "-_"
    )[:64]
