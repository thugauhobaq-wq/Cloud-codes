"""Настройки процесса. Сами розыгрыши заводятся командами из чата."""

from __future__ import annotations

from botkit import BotSettings
from pydantic import Field


class Settings(BotSettings):
    # Токен, админы, путь к базе и уровень логов уже есть в BotSettings.

    project_name: str = Field(default="Розыгрыши")
    bot_username: str = Field(default="", description="Имя бота без @, нужно для ссылок")

    #: Куда публиковать объявления о победителях: @channel или -100…
    #: Пусто — бот пришлёт готовый текст владельцу, тот опубликует сам.
    announce_chat: str = Field(default="")

    #: Каналы, подписка на которые нужна по умолчанию: через запятую.
    #: У каждого розыгрыша может быть свой список; этот — заготовка.
    default_channels: str = Field(default="")

    #: Как часто проверять, не пора ли подводить итоги.
    deadline_interval_sec: int = Field(default=60, ge=10)

    #: Сколько ждать ответа победителя, прежде чем предложить перевыбор.
    claim_hours: int = Field(default=48, ge=1)

    db_path: str = Field(default="data/giveaway.db")

    def channels(self) -> list[str]:
        return parse_channels(self.default_channels)

    def start_link(self, giveaway_id: int) -> str:
        """Ссылка для поста в канале: по ней человек попадает сразу к розыгрышу."""
        if not self.bot_username:
            return f"(укажите BOT_USERNAME в .env) ?start=g{giveaway_id}"
        return f"https://t.me/{self.bot_username}?start=g{giveaway_id}"


def parse_channels(raw: str) -> list[str]:
    """«@one, @two» → ['@one', '@two'].

    Пользователь пишет каналы как придётся: с @ и без, через запятую или
    пробел, иногда ссылкой. Приводим к виду, который понимает Telegram API.
    """
    items: list[str] = []
    for chunk in (raw or "").replace(";", ",").replace(" ", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if chunk.startswith("https://t.me/"):
            chunk = "@" + chunk.removeprefix("https://t.me/").strip("/")
        elif chunk.startswith("t.me/"):
            chunk = "@" + chunk.removeprefix("t.me/").strip("/")
        if not chunk.startswith("@") and not chunk.lstrip("-").isdigit():
            chunk = "@" + chunk
        if chunk not in items:
            items.append(chunk)
    return items


def load_settings() -> Settings:
    return Settings()
