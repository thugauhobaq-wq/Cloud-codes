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
from importlib import metadata
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
    "docker-entrypoint.sh.tmpl": "docker-entrypoint.sh",
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

#: Файлы сборки монорепозитория: botkit ищется в соседнем каталоге.
MONOREPO_BUILD = ("Dockerfile.tmpl", "docker-compose.yml.tmpl", "ci.yml.tmpl")

#: Чем они заменяются в автономном репозитории, где botkit лежит внутри.
STANDALONE_BUILD = {
    "Dockerfile.standalone.tmpl": "Dockerfile",
    "docker-compose.standalone.tmpl": "docker-compose.yml",
    "ci.standalone.tmpl": ".github/workflows/ci.yml",
}

#: Куда кладётся копия botkit в автономном репозитории.
VENDOR_DIR = "vendor/botkit"


def layout(standalone: bool) -> dict[str, str]:
    """Какие шаблоны разворачивать.

    Различаются только файлы сборки: в монорепозитории botkit лежит рядом с
    проектом, в автономном репозитории — внутри него.
    """
    if not standalone:
        return dict(LAYOUT)
    files = {name: path for name, path in LAYOUT.items() if name not in MONOREPO_BUILD}
    files.update(STANDALONE_BUILD)
    return files

_PACKAGE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class ScaffoldError(RuntimeError):
    """Понятная ошибка генерации — печатается пользователю без трейсбека."""


@dataclass(slots=True)
class Project:
    """Что и куда генерируем."""

    package: str
    directory: Path
    title: str
    standalone: bool = False

    @property
    def name(self) -> str:
        return self.directory.name

    def context(self) -> dict[str, str]:
        return {
            "pkg": self.package,
            "project": self.name,
            "title": self.title,
            **_botkit_context(self.standalone),
        }


def _botkit_context(standalone: bool) -> dict[str, str]:
    """Где в готовом проекте искать botkit — зависит от раскладки."""
    if standalone:
        return {
            "botkit_link": f"{VENDOR_DIR}/",
            "botkit_install": f"pip install -e {VENDOR_DIR}  # каркас, лежит здесь же",
            "docker_note": "",
        }
    return {
        "botkit_link": "../botkit/",
        "botkit_install": "pip install -e ../botkit  # каркас",
        "docker_note": " (собирается из каталога, где лежат и botkit, и этот проект)",
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
    standalone: bool = False,
) -> Project:
    """Создать проект и вернуть описание созданного.

    `directory` по умолчанию — `<пакет>-bot`: так каталог не путается с
    именем пакета внутри `src/`.

    `standalone` кладёт в проект копию botkit и настраивает сборку на неё.
    Это нужно для отдельного репозитория: botkit не опубликован в PyPI, и без
    копии такой репозиторий не собрался бы ни у кого, кроме автора.
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
        shutil.rmtree(target / VENDOR_DIR, ignore_errors=True)

    project = Project(
        package=package,
        directory=target,
        title=title or package.capitalize(),
        standalone=standalone,
    )
    context = project.context()

    for source, destination in layout(standalone).items():
        template = TEMPLATE_DIR / source
        if not template.exists():
            raise ScaffoldError(f"шаблон повреждён: нет файла {source}")

        path = target / destination.format(pkg=package)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(template.read_text(encoding="utf-8"), context), encoding="utf-8")

    if standalone:
        vendor_botkit(target)

    return project


# ── копия botkit внутри проекта ───────────────────────────────────────────


def botkit_sources() -> Path:
    """Каталог пакета botkit — то, что копируется в проект."""
    return Path(__file__).resolve().parent


def botkit_root() -> Path | None:
    """Корень репозитория botkit, если он доступен.

    Есть при установке из исходников (`pip install -e ../botkit`) и нет,
    когда botkit поставлен колесом: тогда `pyproject.toml` собирается по
    метаданным пакета.
    """
    root = botkit_sources().parents[1]
    if (root / "pyproject.toml").is_file() and (root / "src" / "botkit").is_dir():
        return root
    return None


def vendor_botkit(target: Path) -> Path:
    """Положить копию botkit в `vendor/botkit` внутри проекта.

    Без этого отдельный репозиторий не собирается: botkit не в PyPI, а
    Dockerfile и CI сгенерированного бота ставят его из исходников.
    """
    destination = target / VENDOR_DIR
    # Копию всегда пересобираем целиком: устаревшие файлы каркаса опаснее,
    # чем лишние секунды на копирование.
    shutil.rmtree(destination, ignore_errors=True)
    (destination / "src").mkdir(parents=True, exist_ok=True)

    shutil.copytree(
        botkit_sources(),
        destination / "src" / "botkit",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
    )

    root = botkit_root()
    if root is not None:
        shutil.copy2(root / "pyproject.toml", destination / "pyproject.toml")
        shutil.copy2(root / "README.md", destination / "README.md")
    else:
        (destination / "pyproject.toml").write_text(_vendor_pyproject(), encoding="utf-8")
        (destination / "README.md").write_text(
            "# botkit\n\nКопия каркаса для автономной сборки этого бота.\n", encoding="utf-8"
        )
    return destination


def _vendor_pyproject() -> str:
    """Собрать `pyproject.toml` для копии по метаданным установленного botkit."""
    try:
        distribution = metadata.distribution("botkit")
    except metadata.PackageNotFoundError:
        raise ScaffoldError(
            "не нашёл исходники botkit для копии в проект. "
            "Поставьте botkit: pip install -e path/to/botkit"
        ) from None

    # Зависимости дополнительных наборов (`dev`) копии не нужны: она ставится
    # как библиотека, а не как проект для разработки.
    requires = [item for item in (distribution.requires or []) if "extra ==" not in item]
    dependencies = "".join(f'    "{item}",\n' for item in requires)
    return f"""[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "botkit"
version = "{distribution.version}"
description = "Каркас Telegram-ботов — копия внутри проекта"
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
{dependencies}]

[tool.hatch.build.targets.wheel]
packages = ["src/botkit"]
"""


def next_steps(project: Project) -> str:
    install_line = (
        f"pip install -e {VENDOR_DIR}   # каркас лежит в самом проекте"
        if project.standalone
        else "pip install -e ../botkit    # каркас лежит рядом"
    )
    return f"""Готово: {project.directory}

Дальше:
  cd {project.directory}
  cp .env.example .env          # впишите BOT_TOKEN и OWNER_ID
  {install_line}
  pip install -e ".[dev]"
  pytest -q                     # тесты заготовки должны пройти
  python -m {project.package} run

Что уже работает: приём заявок с телефоном, уведомление владельцу с кнопками,
админка со статистикой и рассылкой, напоминание о заявках без ответа.
Правьте под задачу — начните с {project.package}/models.py и handlers/client.py."""
