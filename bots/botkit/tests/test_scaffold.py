from __future__ import annotations

import compileall
import subprocess
import sys
from pathlib import Path

import pytest

from botkit.__main__ import main
from botkit.scaffold import LAYOUT, ScaffoldError, check_package_name, create_project, render


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
