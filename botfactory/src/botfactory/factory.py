"""Сборка бота: шаблон botkit на диск, потом публикация в GitHub.

Работа синхронная и небыстрая — `pip` здесь не участвует, но `git push` ходит
в сеть. Поэтому всё выполняется в отдельном потоке: пока собирается один бот,
остальные сообщения должны обрабатываться.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from botkit.github import GitHub, GitHubError
from botkit.publish import (
    MODE_BRANCH,
    MODE_REPO,
    Published,
    PublishError,
    publish,
    slug,
)
from botkit.scaffold import ScaffoldError, create_project

from .config import Settings
from .orders import Order

log = logging.getLogger(__name__)


class FactoryError(RuntimeError):
    """Сборка не удалась. Текст показывается заказчику как есть."""


@dataclass(frozen=True, slots=True)
class Result:
    """Что получилось: каталог на диске и ссылка в GitHub."""

    directory: Path
    published: Published

    @property
    def url(self) -> str:
        return self.published.url

    @property
    def repo(self) -> str:
        return self.published.repo


class Factory:
    """Один заказ — один вызов `build`."""

    def __init__(self, settings: Settings, *, api: GitHub | None = None) -> None:
        self.settings = settings
        # Клиент можно подменить в тестах — иначе они пошли бы в GitHub.
        self._api = api

    def target_dir(self, order: Order) -> Path:
        """Куда генерировать проект.

        В режиме ветки — прямо в рабочую копию репозитория: именно оттуда
        каталог уедет в коммит. В режиме отдельного репозитория — в рабочий
        каталог фабрики, чтобы не мусорить в чужом дереве.
        """
        name = f"{order.package.replace('_', '-')}-bot"
        root = self.settings.repo_root if order.mode == MODE_BRANCH else self.settings.workdir
        return Path(root) / name

    def build_sync(self, order: Order) -> Result:
        """Сгенерировать и опубликовать. Блокирующая часть работы."""
        target = self.target_dir(order)
        try:
            project = create_project(
                order.package,
                parent=target.parent,
                directory=target.name,
                title=order.title,
                # Повторный заказ того же бота перезаписывает свой же код, но
                # чужие файлы в каталоге не трогает.
                force=True,
                # Отдельный репозиторий уезжает к заказчику один, без botkit
                # рядом, поэтому каркас кладётся внутрь проекта.
                standalone=order.mode == MODE_REPO,
            )
        except ScaffoldError as exc:
            raise FactoryError(str(exc)) from None

        try:
            published = publish(
                project.directory,
                mode=order.mode,
                token=self.settings.github_token,
                name=order.repo or project.name,
                owner=self.settings.github_owner,
                private=order.private,
                description=order.title,
                branch=f"bot/{slug(order.repo or project.name)}"
                if order.mode == MODE_BRANCH
                else "main",
                message=f"Добавить {project.name}: {order.title}",
                repo_root=self.settings.repo_root,
                api=self._api,
            )
        except (PublishError, GitHubError) as exc:
            raise FactoryError(str(exc)) from None

        log.info("собран %s → %s", project.name, published.url)
        return Result(directory=project.directory, published=published)

    async def build(self, order: Order) -> Result:
        return await asyncio.to_thread(self.build_sync, order)


def mode_title(mode: str) -> str:
    return "отдельный репозиторий" if mode == MODE_REPO else "ветка в текущем репозитории"
