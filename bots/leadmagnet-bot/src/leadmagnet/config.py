"""Настройки процесса: токен, канал для проверки подписки, темп рассылки.

Сами лид-магниты и тексты живут в базе и заводятся из чата.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Telegram ──────────────────────────────────────────────────────────
    bot_token: str = Field(default="", description="Токен бота от @BotFather")
    bot_username: str = Field(default="", description="Имя бота без @, нужно для ссылок")
    owner_id: int = Field(default=0, description="Владелец воронки")
    admin_ids: str = Field(default="", description="Дополнительные админы через запятую")

    # ── Воронка ───────────────────────────────────────────────────────────
    project_name: str = Field(default="Бот-помощник")
    welcome_text: str = Field(
        default="",
        description="Приветствие. Пусто — соберём из списка кодовых слов.",
    )

    #: Канал, на который нужно подписаться перед выдачей. Пусто — не проверяем.
    #: Формат: @channel или -1001234567890. Бот должен быть в нём администратором.
    required_channel: str = Field(default="")
    channel_url: str = Field(default="", description="Ссылка на канал для кнопки")

    # ── Догоняющее сообщение ──────────────────────────────────────────────
    followup_enabled: bool = Field(default=True)
    followup_hours: int = Field(default=24, ge=1, description="Через сколько часов догонять")
    followup_interval_sec: int = Field(
        default=300, ge=30, description="Как часто проверять очередь"
    )

    # ── Рассылка ──────────────────────────────────────────────────────────
    #: Telegram гасит рассылку примерно на 30 сообщениях в секунду; 20 — запас,
    #: при котором бота не ограничивают даже на пиках.
    broadcast_rate: float = Field(default=20.0, gt=0, le=30)
    broadcast_batch_pause: float = Field(default=1.0, ge=0)

    #: Уведомлять владельца о каждом новом подписчике. По умолчанию нет:
    #: у работающей воронки это десятки сообщений в день, и их перестают читать.
    notify_new_leads: bool = Field(default=False)

    db_path: str = Field(default="data/leadmagnet.db")
    log_level: str = Field(default="INFO")

    def admins(self) -> set[int]:
        ids = {self.owner_id} if self.owner_id else set()
        for chunk in self.admin_ids.replace(";", ",").split(","):
            chunk = chunk.strip()
            if chunk.lstrip("-").isdigit():
                ids.add(int(chunk))
        return ids

    @property
    def check_subscription(self) -> bool:
        return bool(self.required_channel)

    def channel_link(self) -> str:
        """Ссылка на канал: явная из настроек или собранная из @username."""
        if self.channel_url:
            return self.channel_url
        if self.required_channel.startswith("@"):
            return f"https://t.me/{self.required_channel[1:]}"
        return ""

    def start_link(self, code: str, source: str = "") -> str:
        """Ссылка вида t.me/bot?start=code__source для размещения в постах."""
        if not self.bot_username:
            return f"(укажите BOT_USERNAME в .env) ?start={code}"
        payload = f"{code}__{source}" if source else code
        return f"https://t.me/{self.bot_username}?start={payload}"

    def require_telegram(self) -> None:
        missing = [
            name
            for name, value in (("BOT_TOKEN", self.bot_token), ("OWNER_ID", self.owner_id))
            if not value
        ]
        if missing:
            raise SystemExit(
                "Не заданы обязательные переменные окружения: "
                + ", ".join(missing)
                + ".\nСкопируй .env.example в .env и заполни их."
            )


def load_settings() -> Settings:
    return Settings()
