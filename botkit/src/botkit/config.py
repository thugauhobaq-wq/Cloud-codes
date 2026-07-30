"""Общие настройки бота.

Наследник добавляет свои поля, а токен, админы, путь к базе и уровень логов
одинаковы во всех ботах — вместе с разбором `ADMIN_IDS` и проверкой того, что
боевой запуск вообще сконфигурирован.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BotSettings(BaseSettings):
    """База для настроек конкретного бота.

        class Settings(BotSettings):
            shop_name: str = "Магазин"
            delivery_price: int = 300
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str = Field(default="", description="Токен бота от @BotFather")
    owner_id: int = Field(default=0, description="Владелец: уведомления и доступ к админке")
    admin_ids: str = Field(default="", description="Дополнительные админы через запятую")

    db_path: str = Field(default="data/bot.db")
    log_level: str = Field(default="INFO")

    def admins(self) -> set[int]:
        """Кому доступна админка. Владелец входит сюда всегда.

        Разделители — запятая или точка с запятой: заказчик копирует id из
        разных мест и разделяет их как придётся.
        """
        ids = {self.owner_id} if self.owner_id else set()
        for chunk in self.admin_ids.replace(";", ",").split(","):
            chunk = chunk.strip()
            if chunk.lstrip("-").isdigit():
                ids.add(int(chunk))
        return ids

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.admins()

    def require(self, *fields: str) -> None:
        """Проверить, что боевой режим сконфигурирован.

        Вызывается только перед запуском бота: офлайн-командам (`seed`,
        отчёты, экспорт) токен не нужен, и требовать его там значило бы мешать
        отладке.
        """
        names = fields or ("bot_token", "owner_id")
        missing = [name.upper() for name in names if not getattr(self, name, None)]
        if missing:
            raise SystemExit(
                "Не заданы обязательные переменные окружения: "
                + ", ".join(missing)
                + ".\nСкопируй .env.example в .env и заполни их."
            )
