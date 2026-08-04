"""Настройки процесса: читаются из окружения и .env один раз при старте.

Всё, что меняется по ходу работы — услуги, расписание, выходные — живёт в
базе и правится командами админа. Здесь только то, что задаётся при
разворачивании.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Telegram ──────────────────────────────────────────────────────────
    bot_token: str = Field(default="", description="Токен бота от @BotFather")
    owner_id: int = Field(default=0, description="Главный админ, ему уходят уведомления")
    admin_ids: str = Field(default="", description="Дополнительные админы через запятую")

    # ── Салон ─────────────────────────────────────────────────────────────
    business_name: str = Field(default="Студия")
    business_address: str = Field(default="")
    business_phone: str = Field(default="")
    timezone: str = Field(default="Europe/Moscow")

    # ── Правила записи ────────────────────────────────────────────────────
    horizon_days: int = Field(default=14, ge=1, le=90, description="На сколько дней открыта запись")
    slot_step_min: int = Field(default=30, ge=5, le=120, description="Шаг сетки слотов, минуты")
    min_lead_min: int = Field(
        default=60, ge=0, description="Нельзя записаться позже чем за столько минут до начала"
    )
    cancel_deadline_min: int = Field(
        default=120, ge=0, description="За сколько минут закрывается самостоятельная отмена"
    )
    reminder_lead_min: int = Field(default=60, ge=0, description="За сколько минут напоминать")
    reminder_interval_sec: int = Field(default=60, ge=10, description="Как часто проверять очередь")

    db_path: str = Field(default="data/booking.db")
    log_level: str = Field(default="INFO")

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"Неизвестный часовой пояс {value!r}. Пример: Europe/Moscow"
            ) from exc
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def admins(self) -> set[int]:
        """Кому доступна админка. Владелец входит сюда всегда."""
        ids = {self.owner_id} if self.owner_id else set()
        for chunk in self.admin_ids.replace(";", ",").split(","):
            chunk = chunk.strip()
            if chunk.lstrip("-").isdigit():
                ids.add(int(chunk))
        return ids

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
