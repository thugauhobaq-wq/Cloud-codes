"""Общие приспособления для тестов."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proposals.pdf import FontError, find_family


@pytest.fixture(scope="session")
def family():
    """Семейство шрифтов из системы.

    Без кириллического шрифта собрать PDF нечем, поэтому такие тесты
    пропускаем, а не роняем — на голом CI-образе шрифтов может не быть.
    """
    try:
        return find_family()
    except FontError as error:
        pytest.skip(str(error))
