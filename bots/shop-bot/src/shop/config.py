"""Настройки процесса: токен, реквизиты магазина, правила доставки.

Каталог, цены и остатки живут в базе и правятся из чата — здесь только то,
что задаётся один раз при разворачивании.
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
    owner_id: int = Field(default=0, description="Владелец: ему приходят заказы")
    admin_ids: str = Field(default="", description="Дополнительные админы через запятую")

    # ── Магазин ───────────────────────────────────────────────────────────
    shop_name: str = Field(default="Магазин")
    shop_about: str = Field(default="")
    contacts: str = Field(default="")
    currency: str = Field(default="₽")

    # ── Доставка ──────────────────────────────────────────────────────────
    pickup_address: str = Field(default="", description="Адрес самовывоза; пусто — только доставка")
    delivery_enabled: bool = Field(default=True)
    delivery_price: int = Field(default=300, ge=0)
    free_delivery_from: int = Field(
        default=3000, ge=0, description="Сумма бесплатной доставки; 0 — не применять"
    )
    min_order: int = Field(default=0, ge=0, description="Минимальная сумма заказа; 0 — без порога")

    # ── Оплата ────────────────────────────────────────────────────────────
    #: Токен платёжного провайдера от @BotFather (ЮKassa, Сбербанк, Stripe…).
    #: Пусто — онлайн-оплаты нет, заказы оплачиваются при получении.
    payment_provider_token: str = Field(default="")
    payment_provider_name: str = Field(
        default="", description="Название провайдера для текста: ЮKassa, Сбербанк"
    )
    #: Код валюты по ISO 4217 — для платежей нужен именно он, а не символ.
    payment_currency: str = Field(default="RUB", min_length=3, max_length=3)
    cash_payment_enabled: bool = Field(
        default=True, description="Разрешить оплату при получении"
    )

    # ── Витрина ───────────────────────────────────────────────────────────
    page_size: int = Field(default=6, ge=1, le=20, description="Товаров на странице каталога")
    track_stock: bool = Field(
        default=True, description="Списывать остатки и прятать то, что закончилось"
    )

    db_path: str = Field(default="data/shop.db")
    log_level: str = Field(default="INFO")

    @property
    def online_payment_enabled(self) -> bool:
        """Онлайн-оплата включена ровно тогда, когда есть токен провайдера."""
        return bool(self.payment_provider_token)

    def payment_choices(self) -> list[str]:
        """Способы оплаты, доступные покупателю.

        Пустой список означает, что об оплате бот не спрашивает вовсе:
        продавец договаривается сам, как было до подключения платежей.
        """
        choices: list[str] = []
        if self.online_payment_enabled:
            choices.append("online")
        if self.cash_payment_enabled:
            choices.append("cash")
        return choices

    def admins(self) -> set[int]:
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
