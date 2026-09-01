from __future__ import annotations

import compileall
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from botkit.__main__ import main
from botkit.scaffold import (
    LAYOUT,
    VENDOR_DIR,
    ScaffoldError,
    check_package_name,
    create_project,
    render,
)


def test_project_is_created_with_every_file(tmp_path: Path):
    project = create_project("shop", parent=tmp_path, title="Магазин у дома")

    assert project.directory == tmp_path / "shop-bot"
    for destination in LAYOUT.values():
        assert (project.directory / destination.format(pkg="shop")).exists()


def test_placeholders_are_replaced_everywhere(tmp_path: Path):
    project = create_project("shop", parent=tmp_path, title="Магазин у дома")

    for path in project.directory.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert "{{" not in text, f"остался плейсхолдер в {path.name}"

    readme = (project.directory / "README.md").read_text(encoding="utf-8")
    assert "Магазин у дома" in readme
    assert "python -m shop" in readme


def test_generated_code_compiles(tmp_path: Path):
    """Шаблон должен давать рабочий Python, а не текст, похожий на код."""
    project = create_project("shop", parent=tmp_path)
    assert compileall.compile_dir(str(project.directory / "src"), quiet=2)
    assert compileall.compile_dir(str(project.directory / "tests"), quiet=2)


def test_generated_project_imports_and_runs(tmp_path: Path):
    """Сгенерированный бот должен запускаться: команды работают без токена."""
    project = create_project("shop", parent=tmp_path)
    env_path = str(project.directory / "src")

    result = subprocess.run(
        [sys.executable, "-m", "shop", "stats", "7"],
        cwd=project.directory,
        env={"PYTHONPATH": env_path, "PATH": "/usr/bin:/bin", "DB_PATH": str(tmp_path / "s.db")},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "заявок" in result.stdout


def test_default_directory_name_is_derived_from_the_package(tmp_path: Path):
    project = create_project("lead_magnet", parent=tmp_path)
    assert project.directory.name == "lead-magnet-bot"
    assert (project.directory / "src" / "lead_magnet" / "config.py").exists()


def test_custom_directory(tmp_path: Path):
    project = create_project("shop", parent=tmp_path, directory="my-shop")
    assert project.directory == tmp_path / "my-shop"


def test_existing_non_empty_directory_is_protected(tmp_path: Path):
    target = tmp_path / "shop-bot"
    target.mkdir()
    (target / "important.txt").write_text("не трогать")

    with pytest.raises(ScaffoldError, match="уже существует"):
        create_project("shop", parent=tmp_path)

    # --force перезаписывает код, но чужие файлы не трогает.
    create_project("shop", parent=tmp_path, force=True)
    assert (target / "important.txt").read_text() == "не трогать"
    assert (target / "pyproject.toml").exists()


@pytest.mark.parametrize("name", ["Shop Bot", "123shop", "", "shop-bot!", "botkit"])
def test_bad_package_names_are_rejected(name: str):
    with pytest.raises(ScaffoldError):
        check_package_name(name)


def test_dashes_become_underscores():
    assert check_package_name("lead-magnet") == "lead_magnet"


def test_render_replaces_all_keys():
    assert render("{{pkg}} и {{pkg}} в {{project}}", {"pkg": "shop", "project": "shop-bot"}) == (
        "shop и shop в shop-bot"
    )


def test_cli_creates_a_project(tmp_path: Path, capsys):
    code = main(["new", "booking", "--dir", str(tmp_path), "--title", "Запись"])

    assert code == 0
    assert (tmp_path / "booking-bot" / "pyproject.toml").exists()
    assert "cd" in capsys.readouterr().out


def test_cli_reports_a_bad_name_without_a_traceback(tmp_path: Path, capsys):
    code = main(["new", "Плохое Имя", "--dir", str(tmp_path)])

    assert code == 1
    assert "Не получилось" in capsys.readouterr().out


# ── автономный репозиторий ────────────────────────────────────────────────


def test_standalone_project_carries_a_copy_of_botkit(tmp_path: Path):
    """Отдельный репозиторий уезжает к заказчику один: botkit должен быть внутри."""
    project = create_project("shop", parent=tmp_path, standalone=True)

    vendor = project.directory / VENDOR_DIR
    assert (vendor / "src" / "botkit" / "__init__.py").is_file()
    assert (vendor / "src" / "botkit" / "storage.py").is_file()
    # Без README сборка копии падает: hatchling требует файл из поля readme.
    assert (vendor / "pyproject.toml").is_file()
    assert (vendor / "README.md").is_file()


def test_standalone_build_files_do_not_look_outside_the_repository(tmp_path: Path):
    """Именно на этом ломалась передача: сборка искала botkit уровнем выше."""
    project = create_project("shop", parent=tmp_path, standalone=True)

    dockerfile = (project.directory / "Dockerfile").read_text(encoding="utf-8")
    compose = (project.directory / "docker-compose.yml").read_text(encoding="utf-8")
    workflow = (project.directory / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "COPY vendor/botkit" in dockerfile
    assert "COPY botkit " not in dockerfile
    assert "context: ." in compose and "context: .." not in compose
    assert "../botkit" not in workflow


def test_standalone_readme_points_inside_the_project(tmp_path: Path):
    project = create_project("shop", parent=tmp_path, standalone=True)

    readme = (project.directory / "README.md").read_text(encoding="utf-8")

    assert f"pip install -e {VENDOR_DIR}" in readme
    assert "../botkit" not in readme


def test_vendored_botkit_is_valid_python_and_declares_its_dependencies(tmp_path: Path):
    project = create_project("shop", parent=tmp_path, standalone=True)
    vendor = project.directory / VENDOR_DIR

    assert compileall.compile_dir(str(vendor / "src"), quiet=2)
    manifest = tomllib.loads((vendor / "pyproject.toml").read_text(encoding="utf-8"))
    assert manifest["project"]["name"] == "botkit"
    assert any("aiogram" in item for item in manifest["project"]["dependencies"])
    assert manifest["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/botkit"]


def test_ordinary_project_stays_without_a_copy(tmp_path: Path):
    """В монорепозитории копия не нужна: botkit лежит рядом, дублировать вредно."""
    project = create_project("shop", parent=tmp_path)

    assert not (project.directory / "vendor").exists()
    assert "COPY botkit" in (project.directory / "Dockerfile").read_text(encoding="utf-8")


def test_regenerating_replaces_the_copy_instead_of_mixing_versions(tmp_path: Path):
    project = create_project("shop", parent=tmp_path, standalone=True)
    stale = project.directory / VENDOR_DIR / "src" / "botkit" / "outdated.py"
    stale.write_text("# остаток прошлой версии", encoding="utf-8")

    create_project("shop", parent=tmp_path, standalone=True, force=True)

    assert not stale.exists()


def test_pyproject_for_the_copy_is_built_from_package_metadata(tmp_path: Path, monkeypatch):
    """Когда botkit поставлен колесом, исходного pyproject.toml рядом нет."""
    monkeypatch.setattr("botkit.scaffold.botkit_root", lambda: None)

    project = create_project("shop", parent=tmp_path, standalone=True)

    manifest = tomllib.loads(
        (project.directory / VENDOR_DIR / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert manifest["project"]["version"]
    assert any("aiogram" in item for item in manifest["project"]["dependencies"])
    # Наборы для разработки копии не нужны — она ставится как библиотека.
    assert not any("pytest" in item for item in manifest["project"]["dependencies"])


def test_cli_makes_a_standalone_project(tmp_path: Path, capsys):
    code = main(["new", "shop", "--dir", str(tmp_path), "--standalone"])

    assert code == 0
    assert (tmp_path / "shop-bot" / VENDOR_DIR / "pyproject.toml").is_file()
    assert VENDOR_DIR in capsys.readouterr().out


def test_entrypoint_is_generated_and_wired(tmp_path: Path):
    """Без него бот в контейнере не сможет писать в смонтированный /app/data."""
    project = create_project("shop", parent=tmp_path)
    entrypoint = project.directory / "docker-entrypoint.sh"

    assert entrypoint.exists()
    # Понижение привилегий через setpriv: runuser и su заводят сессию с
    # собственным шеллом, который съедает вывод и глушит SIGTERM.
    assert "setpriv" in entrypoint.read_text()

    dockerfile = (project.directory / "Dockerfile").read_text()
    assert "docker-entrypoint.sh" in dockerfile
    assert 'ENTRYPOINT ["docker-entrypoint.sh"]' in dockerfile


def test_standalone_dockerfile_copies_entrypoint_from_its_own_root(tmp_path: Path):
    """У автономного проекта контекст сборки — его корень, без префикса каталога."""
    project = create_project("solo", parent=tmp_path, standalone=True)
    dockerfile = (project.directory / "Dockerfile").read_text()

    assert "COPY docker-entrypoint.sh" in dockerfile
    assert "COPY solo-bot/docker-entrypoint.sh" not in dockerfile
