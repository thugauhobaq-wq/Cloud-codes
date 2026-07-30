"""Генератор нового бота из шаблона.

Шаблон — не абстрактная заготовка, а работающий бот приёма заявок: меню,
форма с телефоном, уведомление владельцу с кнопками, админка, напоминание о
просроченных заявках, тесты и Docker. Дальше его правят под задачу, а не
собирают с нуля.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

TEMPLATE_DIR = Path(__file__).parent / "template"

#: Файл шаблона → путь в новом проекте. `{pkg}` подставляется в путь.
LAYOUT = {
    "pyproject.toml.tmpl": "pyproject.toml",
    "README.md.tmpl": "README.md",
    "env.example.tmpl": ".env.example",
    "gitignore.tmpl": ".gitignore",
    "Dockerfile.tmpl": "Dockerfile",
    "docker-compose.yml.tmpl": "docker-compose.yml",
    "ci.yml.tmpl": ".github/workflows/ci.yml",
    "src/init.py.tmpl": "src/{pkg}/__init__.py",
    "src/main.py.tmpl": "src/{pkg}/__main__.py",
    "src/config.py.tmpl": "src/{pkg}/config.py",
    "src/models.py.tmpl": "src/{pkg}/models.py",
    "src/storage.py.tmpl": "src/{pkg}/storage.py",
    "src/texts.py.tmpl": "src/{pkg}/texts.py",
    "src/keyboards.py.tmpl": "src/{pkg}/keyboards.py",
    "src/notify.py.tmpl": "src/{pkg}/notify.py",
    "src/reminders.py.tmpl": "src/{pkg}/reminders.py",
    "src/handlers_init.py.tmpl": "src/{pkg}/handlers/__init__.py",
    "src/handlers_client.py.tmpl": "src/{pkg}/handlers/client.py",
    "src/handlers_admin.py.tmpl": "src/{pkg}/handlers/admin.py",
    "tests/conftest.py.tmpl": "tests/conftest.py",
    "tests/test_storage.py.tmpl": "tests/test_storage.py",
    "tests/test_reminders.py.tmpl": "tests/test_reminders.py",
    "tests/test_texts.py.tmpl": "tests/test_texts.py",
}

_PACKAGE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class ScaffoldError(RuntimeError):
    """Понятная ошибка генерации — печатается пользователю без трейсбека."""


@dataclass(slots=True)
class Project:
    """Что и куда генерируем."""

    package: str
    directory: Path
    title: str

    @property
    def name(self) -> str:
        return self.directory.name

    def context(self) -> dict[str, str]:
        return {
            "pkg": self.package,
            "project": self.name,
            "title": self.title,
        }


def check_package_name(name: str) -> str:
    """Имя пакета должно быть импортируемым, иначе бот не запустится.

    Ошибка здесь дешевле, чем `ModuleNotFoundError` после первой правки.
    """
    package = name.strip().lower().replace("-", "_")
    if not _PACKAGE_RE.match(package):
        raise ScaffoldError(
            f"«{name}» не годится для имени пакета: нужны латиница, цифры и подчёркивание, "
            "начиная с буквы. Например: shop, booking, lead_magnet"
        )
    if package in {"botkit", "test", "tests"}:
        raise ScaffoldError(f"имя «{package}» занято, выберите другое")
    return package


def render(text: str, context: dict[str, str]) -> str:
    for key, value in context.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def create_project(
    package: str,
    *,
    parent: Path | str = ".",
    directory: str | None = None,
    title: str | None = None,
    force: bool = False,
) -> Project:
    """Создать проект и вернуть описание созданного.

    `directory` по умолчанию — `<пакет>-bot`: так каталог не путается с
    именем пакета внутри `src/`.
    """
    package = check_package_name(package)
    target = Path(parent) / (directory or f"{package.replace('_', '-')}-bot")

    if target.exists() and any(target.iterdir()):
        if not force:
            raise ScaffoldError(
                f"каталог {target} уже существует и не пуст. "
                "Выберите другое имя или добавьте --force"
            )
        # Перезаписываем только свои файлы: снести чужой каталог целиком —
        # слишком дорогая цена за опечатку в имени.
        shutil.rmtree(target / "src", ignore_errors=True)

    project = Project(package=package, directory=target, title=title or package.capitalize())
    context = project.context()

    for source, destination in LAYOUT.items():
        template = TEMPLATE_DIR / source
        if not template.exists():
            raise ScaffoldError(f"шаблон повреждён: нет файла {source}")

        path = target / destination.format(pkg=package)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(template.read_text(encoding="utf-8"), context), encoding="utf-8")

    return project


def next_steps(project: Project) -> str:
    return f"""Готово: {project.directory}

Дальше:
  cd {project.directory}
  cp .env.example .env          # впишите BOT_TOKEN и OWNER_ID
  pip install -e ".[dev]"
  pytest -q                     # тесты заготовки должны пройти
  python -m {project.package} run

Что уже работает: приём заявок с телефоном, уведомление владельцу с кнопками,
админка со статистикой и рассылкой, напоминание о заявках без ответа.
Правьте под задачу — начните с {project.package}/models.py и handlers/client.py."""
