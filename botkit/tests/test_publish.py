"""Публикация проверяется на настоящем git и локальном bare-репозитории.

Сети здесь нет: «удалённый» репозиторий — каталог в tmp_path, а GitHub API
подменён заглушкой. Проверяем то, что ломается на самом деле: что пуш дошёл,
что токен никуда не утёк и что чужие изменения не уехали в чужую ветку.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from botkit.__main__ import main
from botkit.github import Repo
from botkit.publish import (
    MODE_BRANCH,
    MODE_REPO,
    PublishError,
    authorized,
    publish,
    publish_branch,
    publish_new_repo,
    redact,
    slug,
    web_url,
)

TOKEN = "ghp_secret"


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
    """GitHub без GitHub: репозиторий «создаётся» рядом, в bare-каталоге."""

    def __init__(self, remote: Path, *, existing: bool = False) -> None:
        self.remote = remote
        self.existing = existing
        self.created: list[tuple[str, str, bool]] = []

    def login(self) -> str:
        return "seller"

    def find_repo(self, owner: str, name: str) -> Repo | None:
        return self._repo(name) if self.existing else None

    def create_repo(self, name, *, owner="", private=True, description="") -> Repo:
        self.created.append((name, description, private))
        return self._repo(name)

    def _repo(self, name: str) -> Repo:
        return Repo(
            owner="seller",
            name=name,
            url=f"https://github.com/seller/{name}",
            clone_url=str(self.remote),
            default_branch="main",
        )


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    """Пустой bare-репозиторий — он играет роль GitHub."""
    path = tmp_path / "remote.git"
    path.mkdir()
    git("init", "--bare", "--initial-branch=main", cwd=path)
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "shop-bot"
    (path / "src").mkdir(parents=True)
    (path / "README.md").write_text("# Магазин", encoding="utf-8")
    (path / "src" / "main.py").write_text("print('привет')\n", encoding="utf-8")
    return path


def files_in(remote: Path, ref: str = "main") -> list[str]:
    listing = git("ls-tree", "-r", "--name-only", ref, cwd=remote)
    return sorted(line for line in listing.splitlines() if line)


# ── новый репозиторий ─────────────────────────────────────────────────────


def test_new_repo_is_created_and_pushed(remote: Path, project: Path):
    api = FakeGitHub(remote)

    result = publish_new_repo(
        project, api=api, token=TOKEN, description="Магазин у дома", private=False
    )

    assert api.created == [("shop-bot", "Магазин у дома", False)]
    assert result.created and result.repo == "seller/shop-bot"
    assert result.url == "https://github.com/seller/shop-bot"
    assert files_in(remote) == ["README.md", "src/main.py"]
    assert result.files == 2


def test_repo_name_is_taken_from_the_directory_or_the_option(remote: Path, project: Path):
    api = FakeGitHub(remote)

    publish_new_repo(project, api=api, token=TOKEN, name="Магазин у дома 2024")

    # Имя приводится к допустимому: GitHub сделал бы это молча, и ссылка в
    # сообщении бота вела бы не туда.
    assert api.created[0][0] == "magazin-u-doma-2024"


def test_existing_repo_stops_the_publication(remote: Path, project: Path):
    api = FakeGitHub(remote, existing=True)

    with pytest.raises(PublishError, match="уже существует"):
        publish_new_repo(project, api=api, token=TOKEN)

    assert api.created == []


def test_missing_directory_is_reported(tmp_path: Path, remote: Path):
    with pytest.raises(PublishError, match="не найден"):
        publish_new_repo(tmp_path / "нет-такого", api=FakeGitHub(remote), token=TOKEN)


def test_token_does_not_stay_in_the_git_config(remote: Path, project: Path):
    publish_new_repo(project, api=FakeGitHub(remote), token=TOKEN)

    assert TOKEN not in (project / ".git" / "config").read_text(encoding="utf-8")


def test_empty_project_is_not_committed(tmp_path: Path, remote: Path):
    empty = tmp_path / "empty-bot"
    empty.mkdir()

    with pytest.raises(PublishError, match="нечего коммитить"):
        publish_new_repo(empty, api=FakeGitHub(remote), token=TOKEN)


# ── ветка в существующем репозитории ──────────────────────────────────────


@pytest.fixture
def workdir(tmp_path: Path, remote: Path) -> Path:
    """Клон с первым коммитом — как репозиторий, куда добавляют новых ботов."""
    path = tmp_path / "cloud-codes"
    path.mkdir()
    git("init", "--initial-branch=main", cwd=path)
    (path / "README.md").write_text("# Репозиторий", encoding="utf-8")
    git("add", "-A", cwd=path)
    git("commit", "-m", "первый", cwd=path)
    git("remote", "add", "origin", str(remote), cwd=path)
    git("push", "origin", "main", cwd=path)
    return path


def test_branch_is_pushed_to_the_existing_repository(workdir: Path, remote: Path):
    project = workdir / "shop-bot"
    project.mkdir()
    (project / "README.md").write_text("# Магазин", encoding="utf-8")

    result = publish_branch(project, branch="bot/shop", message="Добавить магазин", token=TOKEN)

    assert not result.created
    assert result.branch == "bot/shop"
    assert files_in(remote, "bot/shop") == ["README.md", "shop-bot/README.md"]
    assert result.url.endswith("/tree/bot/shop")


def test_branch_publication_repeats_without_falling(workdir: Path, remote: Path):
    project = workdir / "shop-bot"
    project.mkdir()
    (project / "README.md").write_text("# Магазин", encoding="utf-8")
    publish_branch(project, branch="bot/shop", message="Добавить магазин", token=TOKEN)

    (project / "README.md").write_text("# Магазин у дома", encoding="utf-8")
    publish_branch(project, branch="bot/shop", message="Поправить название", token=TOKEN)

    assert git("show", "bot/shop:shop-bot/README.md", cwd=remote) == "# Магазин у дома"


def test_publication_after_a_failed_push_finishes_the_job(workdir: Path, remote: Path):
    """Пуш не дошёл, коммит остался. Повтор должен доехать, а не упереться."""
    project = workdir / "shop-bot"
    project.mkdir()
    (project / "README.md").write_text("# Магазин", encoding="utf-8")
    publish_branch(project, branch="bot/shop", message="Добавить магазин", token=TOKEN)

    result = publish_branch(project, branch="bot/shop", message="Добавить магазин", token=TOKEN)

    assert result.files == 0
    assert files_in(remote, "bot/shop") == ["README.md", "shop-bot/README.md"]


def test_foreign_changes_do_not_travel_into_the_new_branch(workdir: Path):
    project = workdir / "shop-bot"
    project.mkdir()
    (project / "README.md").write_text("# Магазин", encoding="utf-8")
    (workdir / "README.md").write_text("недописанная правка", encoding="utf-8")

    with pytest.raises(PublishError, match="вне каталога проекта"):
        publish_branch(project, branch="bot/shop", message="Добавить магазин", token=TOKEN)


def test_directory_outside_the_repository_is_rejected(workdir: Path, tmp_path: Path):
    outside = tmp_path / "чужой-бот"
    outside.mkdir()

    with pytest.raises(PublishError, match="вне репозитория"):
        publish_branch(outside, branch="bot/shop", message="…", token=TOKEN, repo_root=workdir)


# ── общая точка входа ─────────────────────────────────────────────────────


def test_publish_dispatches_by_mode(workdir: Path, remote: Path):
    project = workdir / "shop-bot"
    project.mkdir()
    (project / "README.md").write_text("# Магазин", encoding="utf-8")

    result = publish(project, mode=MODE_BRANCH, token=TOKEN, name="shop")

    assert result.branch == "bot/shop"
    assert files_in(remote, "bot/shop")


def test_cli_creates_a_bot_and_publishes_it_in_one_command(workdir: Path, remote: Path, capsys):
    code = main(["new", "shop", "--dir", str(workdir), "--title", "Магазин", "--push", "branch"])

    assert code == 0
    assert "Опубликовано" in capsys.readouterr().out
    assert "shop-bot/pyproject.toml" in files_in(remote, "bot/shop-bot")


def test_cli_publish_without_a_mode_says_what_is_missing(project: Path, capsys):
    code = main(["publish", str(project)])

    assert code == 1
    assert "укажите --push" in capsys.readouterr().out


def test_unknown_mode_is_rejected(project: Path):
    with pytest.raises(PublishError, match="неизвестный режим"):
        publish(project, mode="куда-нибудь", token=TOKEN)


def test_repo_mode_without_a_token_says_what_is_missing(project: Path):
    with pytest.raises(PublishError, match="GITHUB_TOKEN"):
        publish(project, mode=MODE_REPO, token="")


# ── мелочи, на которых утекают секреты ────────────────────────────────────


def test_token_is_cut_out_of_messages():
    assert TOKEN not in redact(f"fatal: {TOKEN} rejected", TOKEN)
    assert redact("remote: https://x-access-token:ghp_x@github.com/a/b.git") == (
        "remote: https://github.com/a/b.git"
    )


def test_token_is_added_only_to_https():
    assert authorized("https://github.com/a/b.git", TOKEN) == (
        f"https://x-access-token:{TOKEN}@github.com/a/b.git"
    )
    assert authorized("git@github.com:a/b.git", TOKEN) == "git@github.com:a/b.git"
    assert authorized("https://github.com/a/b.git", "") == "https://github.com/a/b.git"


@pytest.mark.parametrize(
    ("remote_url", "expected"),
    [
        ("git@github.com:seller/shop-bot.git", "https://github.com/seller/shop-bot"),
        ("https://github.com/seller/shop-bot.git", "https://github.com/seller/shop-bot"),
        (
            "https://x-access-token:ghp_x@github.com/seller/shop-bot.git",
            "https://github.com/seller/shop-bot",
        ),
    ],
)
def test_web_url_is_shown_without_credentials(remote_url: str, expected: str):
    assert web_url(remote_url) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Магазин у дома", "magazin-u-doma"),
        ("Shop Bot", "shop-bot"),
        ("  LEAD magnet ", "lead-magnet"),
    ],
)
def test_slug_keeps_only_what_github_allows(raw: str, expected: str):
    assert slug(raw) == expected


def test_empty_name_is_an_error():
    with pytest.raises(PublishError, match="имя репозитория"):
        slug("...")
