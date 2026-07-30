"""Подставные объекты для тестов бота — чтобы не ходить в Telegram."""

from __future__ import annotations

from dataclasses import dataclass, field

from .broadcast import BLOCKED, FAILED, SENT


@dataclass
class FakeSender:
    """Отправка вместо Telegram: помнит, кому писали, и умеет отказывать.

        sender = FakeSender(blocked={42})
        await Broadcaster(sender).run([1, 42], "Привет")
        assert sender.sent == [(1, "Привет")]
    """

    blocked: set[int] = field(default_factory=set)
    failing: set[int] = field(default_factory=set)
    sent: list[tuple[int, str]] = field(default_factory=list)

    async def __call__(self, chat_id: int, text: str) -> str:
        if chat_id in self.blocked:
            return BLOCKED
        if chat_id in self.failing:
            return FAILED
        self.sent.append((chat_id, text))
        return SENT

    @property
    def texts(self) -> list[str]:
        return [text for _, text in self.sent]

    def to(self, chat_id: int) -> list[str]:
        return [text for target, text in self.sent if target == chat_id]


@dataclass
class FakeBot:
    """Минимальная замена `Bot` для проверки уведомлений.

    Хватает там, где важно «что и кому отправлено», а не как именно это
    сериализуется в запрос к API.
    """

    unreachable: set[int] = field(default_factory=set)
    messages: list[tuple[int, str]] = field(default_factory=list)

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        if chat_id in self.unreachable:
            raise _forbidden(chat_id)
        self.messages.append((chat_id, text))

    def to(self, chat_id: int) -> list[str]:
        return [text for target, text in self.messages if target == chat_id]


def _forbidden(chat_id: int) -> Exception:
    from aiogram.exceptions import TelegramForbiddenError

    return TelegramForbiddenError(
        method=None,  # type: ignore[arg-type]
        message=f"Forbidden: bot was blocked by the user {chat_id}",
    )
