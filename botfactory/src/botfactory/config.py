"""Настройки процесса. Остальное — в заказе и в кнопках под карточкой."""

from __future__ import annotations

from botkit import BotSettings
from botkit.publish import MODE_BRANCH, MODE_REPO
from pydantic import Field


class Settings(BotSettings):
    # Токен, админы, путь к базе и уровень логов уже есть в BotSettings.

    project_name: str = Field(default="Фабрика ботов")

    #: Токен GitHub: personal access token с правом создавать репозитории
    #: (classic — scope «repo», fine-grained — Administration: read/write).
    github_token: str = Field(default="")
    #: Владелец или организация. Пусто — аккаунт владельца токена.
    github_owner: str = Field(default="")

    #: Куда публиковать по умолчанию: «repo» — новый репозиторий,
    #: «branch» — ветка в репозитории, который лежит в `repo_root`.
    publish_mode: str = Field(default=MODE_REPO)
    #: Новые репозитории приватные: код заказчика не должен становиться
    #: публичным просто потому, что так проще.
    private_repos: bool = Field(default=True)

    #: Где генерируются проекты для режима «repo». Каталог переживает
    #: перезапуск: по нему видно, что бот собирал, и туда можно зайти руками.
    workdir: str = Field(default="data/projects")
    #: Рабочая копия репозитория для режима «branch».
    repo_root: str = Field(default=".")

    db_path: str = Field(default="data/botfactory.db")

    def check_publishing(self) -> None:
        """Проверить, что публиковать вообще есть чем.

        Отдельно от `require()`: офлайн-командам (`builds`, `stats`) токен
        GitHub не нужен, а боту без него нечего делать.
        """
        if self.publish_mode not in (MODE_REPO, MODE_BRANCH):
            raise SystemExit(
                f"PUBLISH_MODE={self.publish_mode!r} — нужно «{MODE_REPO}» или «{MODE_BRANCH}»"
            )
        if not self.github_token:
            raise SystemExit(
                "Не задан GITHUB_TOKEN.\nСоздайте токен с правом на репозитории "
                "и впишите его в .env — без него бот не сможет ничего запушить."
            )


def load_settings() -> Settings:
    return Settings()
