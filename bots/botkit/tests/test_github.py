from __future__ import annotations

import io
import json
import urllib.error

import pytest

from botkit.github import GitHub, GitHubError, Repo

REPO_JSON = {
    "name": "shop-bot",
    "owner": {"login": "seller"},
    "html_url": "https://github.com/seller/shop-bot",
    "clone_url": "https://github.com/seller/shop-bot.git",
    "default_branch": "main",
    "private": True,
}


class FakeHTTP:
    """Подмена `urlopen`: помнит запросы и отдаёт заготовленные ответы."""

    def __init__(self, responses: dict[tuple[str, str], object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, request, timeout=None):
        path = request.full_url.removeprefix("https://api.github.com")
        payload = json.loads(request.data.decode()) if request.data else None
        self.calls.append((request.get_method(), path, payload))
        assert request.get_header("Authorization") == "Bearer secret"

        answer = self.responses.get((request.get_method(), path))
        if answer is None:
            raise _http_error(path, 404, {"message": "Not Found"})
        if isinstance(answer, BaseException):
            raise answer
        return _body(json.dumps(answer))


def _body(text: str) -> io.BytesIO:
    # BytesIO уже контекстный менеджер с `read()` — как раз то, чего ждёт
    # `with urlopen(...) as response`.
    return io.BytesIO(text.encode())


def _http_error(path: str, code: int, payload: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        f"https://api.github.com{path}",
        code,
        "error",
        {},  # type: ignore[arg-type]
        io.BytesIO(json.dumps(payload).encode()),
    )


@pytest.fixture
def http(monkeypatch):
    def install(responses: dict) -> FakeHTTP:
        fake = FakeHTTP(responses)
        monkeypatch.setattr("botkit.github.urllib.request.urlopen", fake)
        return fake

    return install


def test_empty_token_is_rejected_before_any_request():
    with pytest.raises(GitHubError, match="GITHUB_TOKEN"):
        GitHub("   ")


def test_create_repo_goes_to_the_personal_endpoint(http):
    fake = http(
        {
            ("GET", "/user"): {"login": "seller"},
            ("POST", "/user/repos"): REPO_JSON,
        }
    )

    repo = GitHub("secret").create_repo("shop-bot", description="Магазин")

    assert repo.full_name == "seller/shop-bot"
    assert repo.clone_url == "https://github.com/seller/shop-bot.git"
    method, path, payload = fake.calls[-1]
    assert (method, path) == ("POST", "/user/repos")
    # auto_init создал бы README, который пришлось бы мержить с первым пушем.
    assert payload == {
        "name": "shop-bot",
        "private": True,
        "description": "Магазин",
        "auto_init": False,
    }


def test_create_repo_in_an_organisation_uses_another_endpoint(http):
    fake = http(
        {
            ("GET", "/user"): {"login": "seller"},
            ("POST", "/orgs/acme/repos"): REPO_JSON,
        }
    )

    GitHub("secret").create_repo("shop-bot", owner="acme")

    assert fake.calls[-1][1] == "/orgs/acme/repos"


def test_own_login_as_owner_is_not_treated_as_an_organisation(http):
    fake = http(
        {
            ("GET", "/user"): {"login": "seller"},
            ("POST", "/user/repos"): REPO_JSON,
        }
    )

    GitHub("secret").create_repo("shop-bot", owner="seller")

    assert fake.calls[-1][1] == "/user/repos"


def test_missing_repo_is_none_and_not_an_error(http):
    http({("GET", "/user"): {"login": "seller"}})

    assert GitHub("secret").find_repo("seller", "shop-bot") is None


def test_existing_repo_is_returned(http):
    http({("GET", "/repos/seller/shop-bot"): REPO_JSON})

    repo = GitHub("secret").find_repo("seller", "shop-bot")

    assert repo is not None and repo.url == "https://github.com/seller/shop-bot"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (401, "не принял токен"),
        (403, "нет прав"),
        (422, "уже существует"),
    ],
)
def test_http_errors_become_readable_messages(http, code: int, expected: str):
    http(
        {
            ("GET", "/user"): {"login": "seller"},
            ("POST", "/user/repos"): _http_error("/user/repos", code, {}),
        }
    )

    with pytest.raises(GitHubError, match=expected) as info:
        GitHub("secret").create_repo("shop-bot")
    assert info.value.status == code


def test_validation_detail_from_github_reaches_the_user(http):
    http(
        {
            ("GET", "/user"): {"login": "seller"},
            ("POST", "/user/repos"): _http_error(
                "/user/repos", 422, {"errors": [{"message": "name already exists on this account"}]}
            ),
        }
    )

    with pytest.raises(GitHubError, match="name already exists"):
        GitHub("secret").create_repo("shop-bot")


def test_network_failure_is_reported_without_a_traceback(http, monkeypatch):
    def boom(request, timeout=None):
        raise urllib.error.URLError("сеть недоступна")

    monkeypatch.setattr("botkit.github.urllib.request.urlopen", boom)

    with pytest.raises(GitHubError, match="не достучался"):
        GitHub("secret").login()


def test_empty_repo_reports_the_branch_that_does_not_exist_yet():
    """Пустой репозиторий отдаёт ветку, которую и создаст первый пуш."""
    repo = Repo.from_api({"name": "shop-bot", "owner": {"login": "seller"}})

    assert repo.default_branch == "main"
