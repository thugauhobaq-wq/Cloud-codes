"""Хранение черновиков предложения в JSON.

Одно предложение — один файл: его можно положить в git, отправить коллеге или
открыть руками. Логотипы лежат внутри того же файла в base64, чтобы черновик не
рассыпался при переносе.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .models import Proposal

DEFAULT_DIR = Path.home() / ".proposals"
SUFFIX = ".proposal.json"


class StorageError(RuntimeError):
    """Черновик не читается или не пишется."""


def to_json(proposal: Proposal) -> dict:
    """Предложение → словарь для записи, вместе с логотипами."""
    raw = proposal.to_dict()
    if proposal.sender.logo:
        raw["sender"]["logo"] = base64.b64encode(proposal.sender.logo).decode("ascii")
    if proposal.client.logo:
        raw["client"]["logo"] = base64.b64encode(proposal.client.logo).decode("ascii")
    return raw


def from_json(raw: dict) -> Proposal:
    """Словарь → предложение. Логотипы принимаем и как base64, и как data-URL."""
    proposal = Proposal.from_dict(raw)
    proposal.sender.logo = _decode_logo((raw.get("sender") or {}).get("logo"))
    proposal.client.logo = _decode_logo((raw.get("client") or {}).get("logo"))
    return proposal


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


def load(path: str | Path) -> Proposal:
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


def save(proposal: Proposal, path: str | Path) -> Path:
    file = Path(path).expanduser()
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(
        json.dumps(to_json(proposal), ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
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

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "number": self.number,
            "client": self.client,
            "subject": self.subject,
            "updated_at": self.updated_at.isoformat(timespec="seconds"),
        }


class Drafts:
    """Каталог с черновиками — то, чем пользуется веб-форма."""

    def __init__(self, directory: str | Path = DEFAULT_DIR) -> None:
        self.directory = Path(directory).expanduser()

    def path_for(self, slug: str) -> Path:
        safe = _safe_slug(slug)
        if not safe:
            raise StorageError("пустое имя черновика")
        return self.directory / f"{safe}{SUFFIX}"

    def list(self) -> list[DraftInfo]:
        if not self.directory.is_dir():
            return []
        found: list[DraftInfo] = []
        for file in sorted(self.directory.glob(f"*{SUFFIX}")):
            try:
                raw = json.loads(file.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                continue  # чужой файл в каталоге — не повод падать
            found.append(
                DraftInfo(
                    slug=file.name[: -len(SUFFIX)],
                    path=file,
                    number=str(raw.get("number", "")),
                    client=str((raw.get("client") or {}).get("name", "")),
                    subject=str(raw.get("subject", "")),
                    updated_at=datetime.fromtimestamp(file.stat().st_mtime),
                )
            )
        found.sort(key=lambda info: info.updated_at, reverse=True)
        return found

    def read(self, slug: str) -> Proposal:
        return load(self.path_for(slug))

    def write(self, slug: str, proposal: Proposal) -> Path:
        return save(proposal, self.path_for(slug))

    def delete(self, slug: str) -> bool:
        path = self.path_for(slug)
        if path.is_file():
            path.unlink()
            return True
        return False

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
