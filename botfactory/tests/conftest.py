from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from botkit.github import Repo

from botfactory.config import Settings
from botfactory.storage import Storage


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@localhost", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


class FakeGitHub:
    """GitHub без сети: «созданный» репозиторий — локальный bare-каталог."""

    def __init__(self, remote: Path) -> None:
        self.remote = remote
        self.created: list[tuple[str, bool]] = []

    def login(self) -> str:
        return "seller"

    def find_repo(self, owner: str, name: str) -> Repo | None:
        return None

    def create_repo(self, name, *, owner="", private=True, description="") -> Repo:
        self.created.append((name, private))
        return Repo(
            owner="seller",
            name=name,
            url=f"https://github.com/seller/{name}",
            clone_url=str(self.remote),
            default_branch="main",
        )


def bare(path: Path) -> Path:
    path.mkdir()
    git("init", "--bare", "--initial-branch=main", cwd=path)
    return path


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    """Origin рабочей копии — сюда уезжают ветки в режиме branch."""
    return bare(tmp_path / "remote.git")


@pytest.fixture
def github(tmp_path: Path) -> Path:
    """Пустой репозиторий, который «создаёт» GitHub в режиме repo.

    Отдельно от `remote`: в один и тот же bare две несвязанные истории не
    пушатся, а у настоящего нового репозитория истории и нет.
    """
    return bare(tmp_path / "github.git")


@pytest.fixture
def repo_root(tmp_path: Path, remote: Path) -> Path:
    """Рабочая копия репозитория — в неё публикуются боты в режиме branch."""
    path = tmp_path / "cloud-codes"
    path.mkdir()
    git("init", "--initial-branch=main", cwd=path)
    (path / "README.md").write_text("# Репозиторий", encoding="utf-8")
    git("add", "-A", cwd=path)
    git("commit", "-m", "первый", cwd=path)
    git("remote", "add", "origin", str(remote), cwd=path)
    git("push", "origin", "main", cwd=path)
    return path


@pytest.fixture
def settings(tmp_path: Path, repo_root: Path) -> Settings:
    return Settings(
        bot_token="test",
        owner_id=1,
        db_path=str(tmp_path / "test.db"),
        github_token="ghp_secret",
        workdir=str(tmp_path / "projects"),
        repo_root=str(repo_root),
    )


@pytest_asyncio.fixture
async def storage(settings: Settings) -> AsyncIterator[Storage]:
    store = Storage(settings.db_path)
    await store.open()
    try:
        yield store
    finally:
        await store.close()


def files_in(remote: Path, ref: str = "main") -> list[str]:
    listing = git("ls-tree", "-r", "--name-only", ref, cwd=remote)
    return sorted(line for line in listing.splitlines() if line)
