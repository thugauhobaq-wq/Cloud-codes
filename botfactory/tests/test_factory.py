"""Сборка проверяется целиком: заказ → проект на диске → код в «GitHub».

Роль GitHub играет локальный bare-репозиторий, роль API — заглушка. Так
проверяется то, ради чего бот и написан: после одной фразы код действительно
лежит в репозитории.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from botkit.publish import MODE_BRANCH, MODE_REPO

from botfactory.config import Settings
from botfactory.factory import Factory, FactoryError
from botfactory.orders import Order
from conftest import FakeGitHub, files_in, git


async def test_new_repository_gets_a_working_project(settings: Settings, github: Path):
    api = FakeGitHub(github)
    order = Order(package="shop", title="Магазин у дома", mode=MODE_REPO, private=False)

    result = await Factory(settings, api=api).build(order)

    assert api.created == [("shop-bot", False)]
    assert result.url == "https://github.com/seller/shop-bot"
    assert result.directory == Path(settings.workdir) / "shop-bot"
    published = files_in(github)
    assert "pyproject.toml" in published
    assert "src/shop/handlers/client.py" in published
    assert "tests/test_storage.py" in published
    assert ".github/workflows/ci.yml" in published


async def test_published_repository_is_self_contained(settings: Settings, github: Path):
    """Покупатель клонирует репозиторий один — botkit рядом у него не лежит.

    Именно на этом сборка у заказчика падала: Dockerfile искал каркас
    уровнем выше, а в отдельном репозитории там ничего нет.
    """
    order = Order(package="shop", title="Магазин у дома", mode=MODE_REPO)

    await Factory(settings, api=FakeGitHub(github)).build(order)

    published = files_in(github)
    assert "vendor/botkit/src/botkit/__init__.py" in published
    assert "vendor/botkit/pyproject.toml" in published
    assert "vendor/botkit/README.md" in published


async def test_published_repository_builds_from_its_own_root(settings: Settings, github: Path):
    order = Order(package="shop", title="Магазин", mode=MODE_REPO)

    result = await Factory(settings, api=FakeGitHub(github)).build(order)

    dockerfile = (result.directory / "Dockerfile").read_text(encoding="utf-8")
    compose = (result.directory / "docker-compose.yml").read_text(encoding="utf-8")
    assert "COPY vendor/botkit" in dockerfile
    assert "context: .." not in compose


async def test_branch_mode_keeps_the_shared_botkit(settings: Settings, remote: Path):
    """В общем репозитории botkit уже есть — копия только дублировала бы код."""
    order = Order(package="shop", title="Магазин", mode=MODE_BRANCH)

    result = await Factory(settings).build(order)

    assert not (result.directory / "vendor").exists()
    assert "shop-bot/vendor/botkit/pyproject.toml" not in files_in(remote, "bot/shop-bot")


async def test_branch_mode_puts_the_bot_into_the_existing_repository(
    settings: Settings, remote: Path, repo_root: Path
):
    order = Order(package="shop", title="Магазин у дома", mode=MODE_BRANCH)

    result = await Factory(settings).build(order)

    assert result.published.branch == "bot/shop-bot"
    assert result.directory == repo_root / "shop-bot"
    published = files_in(remote, "bot/shop-bot")
    assert "shop-bot/pyproject.toml" in published
    # Прежнее содержимое репозитория на месте: бот добавляет, а не заменяет.
    assert "README.md" in published


async def test_title_reaches_the_generated_readme(settings: Settings, github: Path):
    order = Order(package="shop", title="Магазин у дома", mode=MODE_REPO)

    result = await Factory(settings, api=FakeGitHub(github)).build(order)

    readme = (result.directory / "README.md").read_text(encoding="utf-8")
    assert "Магазин у дома" in readme


async def test_custom_repository_name_is_used(settings: Settings, github: Path):
    api = FakeGitHub(github)
    order = Order(package="shop", title="Магазин", mode=MODE_REPO, repo="Магазин у дома")

    await Factory(settings, api=api).build(order)

    # Кириллица в имени репозитория недопустима — транслитерируем сами.
    assert api.created == [("magazin-u-doma", True)]


async def test_repeated_order_updates_the_same_branch(settings: Settings, remote: Path):
    """Второй заказ того же бота перезаписывает свой каталог, а не падает."""
    await Factory(settings).build(Order(package="shop", title="Магазин", mode=MODE_BRANCH))

    result = await Factory(settings).build(
        Order(package="shop", title="Магазин у дома", mode=MODE_BRANCH)
    )

    assert result.published.branch == "bot/shop-bot"
    assert "Магазин у дома" in git("show", "bot/shop-bot:shop-bot/README.md", cwd=remote)


async def test_bad_package_name_is_reported_without_a_traceback(settings: Settings, github: Path):
    order = Order(package="botkit", title="Ой", mode=MODE_REPO)

    with pytest.raises(FactoryError, match="занято"):
        await Factory(settings, api=FakeGitHub(github)).build(order)


async def test_missing_token_is_reported_before_anything_is_pushed(settings: Settings):
    settings.github_token = ""
    order = Order(package="shop", title="Магазин", mode=MODE_REPO)

    with pytest.raises(FactoryError, match="GITHUB_TOKEN"):
        await Factory(settings).build(order)


async def test_foreign_changes_stop_the_branch_publication(settings: Settings, repo_root: Path):
    (repo_root / "README.md").write_text("недописанная правка", encoding="utf-8")
    order = Order(package="shop", title="Магазин", mode=MODE_BRANCH)

    with pytest.raises(FactoryError, match="вне каталога проекта"):
        await Factory(settings).build(order)

    # Ветка не создана: чужие правки не уехали в чужой коммит.
    assert git("branch", "--list", "bot/shop-bot", cwd=repo_root) == ""
