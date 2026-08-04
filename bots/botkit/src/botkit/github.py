"""Минимальный клиент GitHub API — ровно то, что нужно для публикации бота.

Стандартная библиотека вместо очередной зависимости: запросов всего три —
кто я, есть ли такой репозиторий, создать репозиторий. Ради них тянуть
`PyGithub` в бота, который и так везёт aiogram, незачем.

Клиент синхронный: сеть здесь занимает доли секунды, а вызывается он из
`asyncio.to_thread`, чтобы не блокировать опрос Telegram.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

API_URL = "https://api.github.com"

#: Таймаут запроса. GitHub отвечает за доли секунды; минута ожидания в боте
#: означала бы, что пользователь успел решить, что всё сломалось.
TIMEOUT = 30


class GitHubError(RuntimeError):
    """Ошибка API, которую не стыдно показать пользователю."""

    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class Repo:
    """Созданный или найденный репозиторий."""

    owner: str
    name: str
    url: str
    clone_url: str
    default_branch: str
    private: bool = True

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @classmethod
    def from_api(cls, data: dict) -> Repo:
        owner = (data.get("owner") or {}).get("login", "")
        return cls(
            owner=owner,
            name=data.get("name", ""),
            url=data.get("html_url", ""),
            clone_url=data.get("clone_url", ""),
            # Пустой репозиторий отдаёт ветку, которой ещё нет: именно её и
            # нужно создать первым пушем.
            default_branch=data.get("default_branch") or "main",
            private=bool(data.get("private", True)),
        )


class GitHub:
    """Клиент GitHub API с токеном.

        api = GitHub(token)
        repo = api.create_repo("shop-bot", description="Магазин у дома")
        print(repo.url)
    """

    def __init__(self, token: str, *, api_url: str = API_URL, timeout: int = TIMEOUT) -> None:
        if not token or not token.strip():
            raise GitHubError(
                "не задан GITHUB_TOKEN. Нужен personal access token с правом "
                "создавать репозитории (classic: scope «repo»; fine-grained: "
                "Administration → Read and write)"
            )
        self._token = token.strip()
        self._api_url = api_url.rstrip("/")
        self._timeout = timeout

    # ── запросы ───────────────────────────────────────────────────────────

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = path if path.startswith("http") else f"{self._api_url}{path}"
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", "botkit")
        if body is not None:
            request.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read().decode("utf-8") or "{}"
        except urllib.error.HTTPError as exc:
            raise self._error(exc) from None
        except urllib.error.URLError as exc:
            raise GitHubError(f"не достучался до GitHub: {exc.reason}") from None

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise GitHubError("GitHub ответил не JSON") from None

    def _error(self, exc: urllib.error.HTTPError) -> GitHubError:
        """Превратить HTTP-ошибку в понятную фразу.

        Пользователю бота бесполезен `HTTP Error 422`: ему нужно знать, что
        репозиторий с таким именем уже есть, а токену не хватает прав.
        """
        try:
            data = json.loads(exc.read().decode("utf-8") or "{}")
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            data = {}
        detail = data.get("message", "")
        errors = data.get("errors") or []
        if errors and isinstance(errors[0], dict) and errors[0].get("message"):
            detail = errors[0]["message"]

        known = {
            401: "GitHub не принял токен: проверьте GITHUB_TOKEN",
            403: "у токена нет прав на это действие",
            404: "GitHub не нашёл такой аккаунт или репозиторий",
            422: f"GitHub отказал: {detail or 'такой репозиторий уже существует'}",
        }
        message = known.get(exc.code) or f"GitHub вернул {exc.code}: {detail or exc.reason}"
        return GitHubError(message, status=exc.code)

    # ── операции ──────────────────────────────────────────────────────────

    def login(self) -> str:
        """Владелец токена. Он же владелец репозитория по умолчанию."""
        return self.request("GET", "/user").get("login", "")

    def find_repo(self, owner: str, name: str) -> Repo | None:
        """Репозиторий или `None`, если его нет.

        Проверяем до создания: понятное «репозиторий уже есть, вот ссылка»
        полезнее, чем ошибка 422 после генерации проекта.
        """
        try:
            return Repo.from_api(self.request("GET", f"/repos/{owner}/{name}"))
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise

    def create_repo(
        self,
        name: str,
        *,
        owner: str = "",
        private: bool = True,
        description: str = "",
    ) -> Repo:
        """Создать репозиторий у владельца токена или в организации.

        Без `auto_init`: первый коммит приходит пушем из сгенерированного
        проекта, а созданный GitHub README пришлось бы с ним мержить.
        """
        payload = {
            "name": name,
            "private": private,
            "description": description[:350],
            "auto_init": False,
        }
        me = self.login()
        # Организация от личного аккаунта отличается только ручкой API;
        # угадывать по имени нельзя, поэтому сверяемся с владельцем токена.
        path = "/user/repos" if not owner or owner == me else f"/orgs/{owner}/repos"
        return Repo.from_api(self.request("POST", path, payload))
