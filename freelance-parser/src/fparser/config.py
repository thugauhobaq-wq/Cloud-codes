"""Настройки процесса, читаются из окружения / .env.

Здесь только то, что задаётся один раз при разворачивании (токены, пути, интервалы).
Пользовательские фильтры живут в БД и меняются командами бота — см. storage.py.
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

    # Telegram
    bot_token: str = Field(default="", description="Токен бота от @BotFather")
    target_chat_id: str = Field(default="", description="Канал/чат для заказов")
    owner_id: int = Field(default=0, description="Кому разрешено менять настройки")

    # Опрос
    poll_interval: int = Field(default=180, ge=30, description="Базовый интервал, секунды")
    db_path: str = Field(default="data/fparser.db")

    # Сеть
    http_proxy_url: str | None = Field(default=None)
    request_timeout: float = Field(default=25.0, gt=0)

    # Подмена адресов лент. Биржи их иногда меняют — тогда починка сводится к
    # строчке в .env вместо правки кода и выкладки новой версии.
    feed_url_kwork: str | None = Field(default=None)
    feed_url_flru: str | None = Field(default=None)
    feed_url_habr: str | None = Field(default=None)
    feed_url_weblancer: str | None = Field(default=None)

    log_level: str = Field(default="INFO")

    def feed_overrides(self) -> dict[str, str | None]:
        """Переопределения адресов, ключ — слаг площадки."""
        return {
            "kwork": self.feed_url_kwork,
            "flru": self.feed_url_flru,
            "habr": self.feed_url_habr,
            "weblancer": self.feed_url_weblancer,
        }

    def require_telegram(self) -> None:
        """Проверить, что боевой режим сконфигурирован.

        Вызывается только в `run` — для `dryrun`/`probe` токен не нужен, и требовать
        его там значило бы мешать отладке парсеров.
        """
        missing = [
            name
            for name, value in (
                ("BOT_TOKEN", self.bot_token),
                ("TARGET_CHAT_ID", self.target_chat_id),
                ("OWNER_ID", self.owner_id),
            )
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
