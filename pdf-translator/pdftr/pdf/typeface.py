"""Выбор шрифта под язык перевода.

DejaVu закрывает латиницу, кириллицу и греческий — этого хватает для доброй
половины пар. Но если переводить на японский или китайский, в DejaVu нужных
знаков нет, и вместо текста получатся пустые квадраты. Поэтому шрифт
подбирается по языку, а перед печатью проверяется, что символы перевода в нём
действительно есть: лучше честно сказать «поставьте шрифт», чем отдать
документ с квадратами.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from .embed import FontError, FontFamily, TrueTypeFont, find_family, load

_SEARCH_DIRS = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "~/.fonts",
    "~/.local/share/fonts",
    "/Library/Fonts",
    "/System/Library/Fonts",
    "~/Library/Fonts",
    "C:/Windows/Fonts",
)

# Кандидаты по письменностям. Внутри каждой — от лучшего к худшему.
_BY_SCRIPT: dict[str, tuple[str, ...]] = {
    "cjk": (
        "NotoSansCJK-Regular.ttc", "NotoSansCJKsc-Regular.otf",
        "NotoSansCJKjp-Regular.otf", "NotoSansCJKkr-Regular.otf",
        "SourceHanSansSC-Regular.otf", "wqy-zenhei.ttc", "wqy-microhei.ttc",
        "fonts-japanese-gothic.ttf", "TakaoPGothic.ttf", "NanumGothic.ttf",
        "msyh.ttc", "simsun.ttc", "PingFang.ttc", "Hiragino Sans GB.ttc",
    ),
    "arabic": (
        "NotoSansArabic-Regular.ttf", "NotoNaskhArabic-Regular.ttf",
        "DejaVuSans.ttf", "amiri-regular.ttf", "arial.ttf",
    ),
    "hebrew": (
        "NotoSansHebrew-Regular.ttf", "DejaVuSans.ttf", "arial.ttf",
    ),
    "devanagari": (
        "NotoSansDevanagari-Regular.ttf", "Lohit-Devanagari.ttf",
        "gargi.ttf", "mangal.ttf",
    ),
    "thai": ("NotoSansThai-Regular.ttf", "Garuda.ttf", "Waree.ttf"),
    "armenian": ("NotoSansArmenian-Regular.ttf", "DejaVuSans.ttf"),
    "georgian": ("NotoSansGeorgian-Regular.ttf", "DejaVuSans.ttf"),
}

_LANGUAGE_SCRIPT = {
    "zh": "cjk", "zh-CN": "cjk", "zh-TW": "cjk", "ja": "cjk", "ko": "cjk",
    "ar": "arabic", "fa": "arabic", "ur": "arabic",
    "he": "hebrew", "yi": "hebrew",
    "hi": "devanagari", "mr": "devanagari", "ne": "devanagari",
    "th": "thai", "hy": "armenian", "ka": "georgian",
}

_PACKAGES = {
    "cjk": "fonts-noto-cjk (Debian/Ubuntu) или google-noto-sans-cjk-fonts (Fedora)",
    "arabic": "fonts-noto-core",
    "hebrew": "fonts-noto-core",
    "devanagari": "fonts-noto-core или fonts-lohit-deva",
    "thai": "fonts-noto-core или fonts-thai-tlwg",
    "armenian": "fonts-noto-core",
    "georgian": "fonts-noto-core",
}


@lru_cache(maxsize=1)
def _index() -> dict[str, Path]:
    """Имя файла шрифта в нижнем регистре → путь. Один обход на весь процесс."""
    found: dict[str, Path] = {}
    for raw in _SEARCH_DIRS:
        root = Path(raw).expanduser()
        if not root.is_dir():
            continue
        try:
            for path in root.rglob("*"):
                if path.suffix.lower() in (".ttf", ".ttc", ".otf"):
                    found.setdefault(path.name.lower(), path)
        except OSError:
            continue
    return found


def script_of(language: str) -> str:
    return _LANGUAGE_SCRIPT.get(language, _LANGUAGE_SCRIPT.get(language.split("-")[0], ""))


def family_for(target: str, preferred: str | Path | None = None) -> FontFamily:
    """Семейство, которым будет напечатан перевод на язык `target`."""
    if preferred:
        return find_family(preferred)
    script = script_of(target)
    if not script:
        return find_family()

    index = _index()
    for name in _BY_SCRIPT.get(script, ()):
        path = index.get(name.lower())
        if path is None:
            continue
        try:
            load(path)
        except FontError:
            continue
        # У шрифтов для иероглифов отдельного жирного файла обычно нет:
        # начертания подменяются одним и тем же, это нормально.
        return FontFamily(name=path.stem, regular=path)
    raise FontError(
        f"нет шрифта для языка «{target}»: поставьте {_PACKAGES.get(script, 'шрифт Noto')} "
        "или укажите свой файл .ttf в настройках."
    )


def uncovered(ttf: TrueTypeFont, text: str, limit: int = 400) -> set[str]:
    """Символы текста, которых в шрифте нет. Смотрим выборку, а не всё подряд."""
    seen: set[str] = set()
    missing: set[str] = set()
    for char in text:
        if char in seen or char.isspace():
            continue
        seen.add(char)
        if not ttf.has(ord(char)):
            missing.add(char)
        if len(seen) >= limit:
            break
    return missing
