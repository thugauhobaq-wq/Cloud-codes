"""Публикация сгенерированного бота в GitHub.

Между «проект создан на диске» и «код лежит в GitHub» каждый раз одни и те
же шаги: `git init`, коммит, репозиторий через API, пуш. Здесь они собраны в
две функции — под новый репозиторий и под ветку в существующем.

Токен в `.git/config` не попадает: он подставляется в адрес одного-единственного
`git push` и вырезается из текста ошибок. Иначе секрет утекал бы в конфиг
репозитория, в логи CI и в сообщение бота.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .github import GitHub, GitHubError, Repo
from .texts import translit

log = logging.getLogger(__name__)

#: Куда публикуем: отдельный репозиторий или ветка в текущем.
MODE_REPO = "repo"
MODE_BRANCH = "branch"
MODES = (MODE_REPO, MODE_BRANCH)

#: Автор коммита. Без него `git commit` в контейнере падает на
#: «Please tell me who you are», а глобальный конфиг там пуст.
AUTHOR_NAME = "botkit"
AUTHOR_EMAIL = "botkit@localhost"

GIT_TIMEOUT = 120

_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


class PublishError(RuntimeError):
    """Понятная ошибка публикации — печатается без трейсбека."""


@dataclass(frozen=True, slots=True)
class Published:
    """Что получилось. `url` — ссылка, которую можно отдать человеку."""

    repo: str
    branch: str
    url: str
    created: bool
    files: int = 0

    def describe(self) -> str:
        what = "новый репозиторий" if self.created else f"ветка {self.branch}"
        return f"{what}: {self.url}"


# ── git ───────────────────────────────────────────────────────────────────


def redact(text: str, token: str = "") -> str:
    """Убрать токен из текста перед логом или сообщением пользователю."""
    if token:
        text = text.replace(token, "***")
    # Токен мог попасть в адрес: https://x-access-token:ghp_…@github.com/…
    return re.sub(r"(https://)[^/@\s]+@", r"\1", text)


def git(*args: str, cwd: Path | str, token: str = "") -> str:
    """Выполнить git и вернуть stdout. Ошибка — `PublishError` без секретов."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        message = detail[-1] if detail else f"код возврата {result.returncode}"
        raise PublishError(redact(f"git {args[0]}: {message}", token))
    return result.stdout.strip()


def git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def slug(name: str) -> str:
    """Имя репозитория из произвольной строки, в том числе русской.

    GitHub заменит недопустимые символы сам, но молча — и кириллическое
    «Магазин у дома» превратится у него в вереницу дефисов, а ссылка в
    сообщении бота будет вести не туда. Поэтому транслитерируем сами.
    """
    value = _SLUG_RE.sub("-", translit(name).strip()).strip("-.")
    if not value:
        raise PublishError("не смог собрать имя репозитория из названия")
    return value[:100]


def authorized(url: str, token: str) -> str:
    """Адрес для одного пуша. Для ssh и пустого токена — как есть."""
    if not token or not url.startswith("https://"):
        return url
    return "https://x-access-token:" + token + "@" + url[len("https://") :]


def web_url(remote: str) -> str:
    """Адрес репозитория для человека из адреса remote."""
    url = remote.strip()
    if url.startswith("git@"):
        host, _, path = url.partition(":")
        url = f"https://{host.removeprefix('git@')}/{path}"
    url = re.sub(r"^https://[^/@]+@", "https://", url)
    return url.removesuffix(".git")


# ── коммит ────────────────────────────────────────────────────────────────


def has_commits(directory: Path) -> bool:
    try:
        git("rev-parse", "--verify", "HEAD", cwd=directory)
    except PublishError:
        return False
    return True


def commit(directory: Path, message: str, *, paths: list[str] | None = None) -> int:
    """Проиндексировать и закоммитить. Возвращает число файлов в коммите.

    Автор задаётся через `-c`, а не `git config`: чужой репозиторий и его
    настройки трогать нельзя, а коммит должен пройти и в пустом контейнере.
    """
    git("add", "--", *(paths or ["."]), cwd=directory)
    staged = git("diff", "--cached", "--name-only", cwd=directory)
    files = [line for line in staged.splitlines() if line]
    if not files:
        if not has_commits(directory):
            raise PublishError("нечего коммитить: в проекте нет файлов")
        # Повтор после сбоя на пуше: коммит уже сделан, а нового ничего нет.
        # Пустой коммит здесь был бы мусором, ошибка — неправдой: пушить есть
        # что, до GitHub это просто не доехало.
        return 0

    git(
        "-c",
        f"user.name={AUTHOR_NAME}",
        "-c",
        f"user.email={AUTHOR_EMAIL}",
        "commit",
        "-m",
        message,
        cwd=directory,
    )
    return len(files)


def init_repo(directory: Path, branch: str = "main") -> None:
    """Завести репозиторий в каталоге проекта, если его там ещё нет."""
    if (directory / ".git").exists():
        return
    git("init", cwd=directory)
    # `git init -b` есть не везде (git < 2.28), а ветку по умолчанию хочется
    # предсказуемую: имя ветки уходит в ссылку и в CI.
    git("symbolic-ref", "HEAD", f"refs/heads/{branch}", cwd=directory)


def toplevel(directory: Path) -> Path:
    try:
        return Path(git("rev-parse", "--show-toplevel", cwd=directory))
    except PublishError:
        raise PublishError(f"{directory} не внутри git-репозитория") from None


def inside(repo_root: Path, directory: Path) -> str:
    """Путь проекта относительно корня репозитория."""
    try:
        return directory.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        raise PublishError(f"каталог {directory} лежит вне репозитория {repo_root}") from None


def dirty_outside(repo_root: Path, keep: Path) -> list[str]:
    """Изменённые файлы вне каталога проекта.

    Смена ветки утащила бы их за собой в чужой коммит, поэтому такую
    публикацию лучше остановить до `git checkout`.
    """
    relative = inside(repo_root, keep)
    changed = []
    for line in git("status", "--porcelain", cwd=repo_root).splitlines():
        # Статус отделён от пути пробелом, но сам может начинаться с пробела
        # («` M файл`»), а `git()` отдаёт вывод уже обрезанным — считать
        # позицию нельзя, режем по первому пробелу.
        path = line.strip().partition(" ")[2].strip().strip('"')
        # Переименование: «старое -> новое», важно новое.
        path = path.rpartition(" -> ")[2] or path
        if path != relative and not path.startswith(f"{relative}/"):
            changed.append(path)
    return changed


# ── публикация ────────────────────────────────────────────────────────────


def publish_new_repo(
    directory: Path | str,
    *,
    api: GitHub,
    token: str,
    name: str = "",
    owner: str = "",
    private: bool = True,
    description: str = "",
    branch: str = "main",
    message: str = "Первый коммит: бот сгенерирован botkit",
) -> Published:
    """Создать репозиторий в GitHub и запушить туда каталог проекта.

    Репозиторий создаётся до коммита: если имя занято или токену не хватает
    прав, узнать об этом лучше раньше, чем после `git commit`.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise PublishError(f"каталог {directory} не найден")

    repo_name = slug(name or directory.name)
    try:
        login = owner or api.login()
        existing = api.find_repo(login, repo_name)
        if existing is not None:
            raise PublishError(
                f"репозиторий {existing.full_name} уже существует: {existing.url}. "
                "Выберите другое имя"
            )
        repo = api.create_repo(
            repo_name, owner=owner, private=private, description=description
        )
    except GitHubError as exc:
        raise PublishError(str(exc)) from None

    init_repo(directory, branch)
    files = commit(directory, message)
    push(directory, repo.clone_url, branch, token=token)

    return Published(
        repo=repo.full_name,
        branch=branch,
        url=repo.url,
        created=True,
        files=files,
    )


def publish_branch(
    directory: Path | str,
    *,
    branch: str,
    message: str,
    token: str = "",
    repo_root: Path | str | None = None,
    remote: str = "origin",
) -> Published:
    """Закоммитить каталог проекта веткой в уже существующем репозитории.

    Так живут боты этого репозитория: каждый — подкаталог, каждый новый —
    ветка, из которой открывается pull request.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise PublishError(f"каталог {directory} не найден")

    root = Path(repo_root) if repo_root else toplevel(directory)
    changed = dirty_outside(root, directory)
    if changed:
        raise PublishError(
            "в репозитории есть незакоммиченные изменения вне каталога проекта: "
            + ", ".join(changed[:5])
            + ". Разберитесь с ними — иначе они уедут в чужую ветку"
        )

    # -B, а не -b: повторная публикация того же бота не должна падать на
    # «ветка уже существует», её содержимое всё равно перезапишется коммитом.
    relative = inside(root, directory)
    git("checkout", "-B", branch, cwd=root)
    files = commit(root, message, paths=[relative])

    remote_url = git("remote", "get-url", remote, cwd=root)
    push(root, remote_url, branch, token=token)

    url = web_url(remote_url)
    return Published(
        repo="/".join(url.split("/")[-2:]),
        branch=branch,
        url=f"{url}/tree/{branch}",
        created=False,
        files=files,
    )


def push(directory: Path, remote_url: str, branch: str, *, token: str = "") -> None:
    """Отправить ветку по адресу с токеном.

    Пушим по адресу, а не через `origin`: так токен не оседает в
    `.git/config`, откуда его потом никто не вычистит.
    """
    git(
        "push",
        authorized(remote_url, token),
        f"HEAD:refs/heads/{branch}",
        cwd=directory,
        token=token,
    )


def publish(
    directory: Path | str,
    *,
    mode: str,
    token: str,
    name: str = "",
    owner: str = "",
    private: bool = True,
    description: str = "",
    branch: str = "",
    message: str = "",
    repo_root: Path | str | None = None,
    api: GitHub | None = None,
) -> Published:
    """Одна точка входа для CLI и бота: режим выбирается параметром."""
    if mode not in MODES:
        raise PublishError(f"неизвестный режим публикации «{mode}», нужен один из: {MODES}")
    if not git_available():
        raise PublishError("git не установлен — публиковать нечем")

    directory = Path(directory)
    title = description or f"Telegram-бот {directory.name}"

    if mode == MODE_REPO:
        try:
            client = api or GitHub(token)
        except GitHubError as exc:
            raise PublishError(str(exc)) from None
        return publish_new_repo(
            directory,
            api=client,
            token=token,
            name=name,
            owner=owner,
            private=private,
            description=title,
            branch=branch or "main",
            message=message or f"Первый коммит: {title}",
        )

    return publish_branch(
        directory,
        branch=branch or f"bot/{slug(name or directory.name)}",
        message=message or f"Добавить {directory.name}: {title}",
        token=token,
        repo_root=repo_root,
    )


__all__ = [
    "MODES",
    "MODE_BRANCH",
    "MODE_REPO",
    "GitHub",
    "PublishError",
    "Published",
    "Repo",
    "publish",
    "publish_branch",
    "publish_new_repo",
    "slug",
]
