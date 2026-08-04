"""Перевод документа целиком: от байтов на входе до байтов на выходе.

Здесь собирается вся цепочка и, главное, задаётся порядок работы. Он выбран под
большие файлы: страницы разбираются пачками, перевод пачки уходит одним заходом,
прогресс сообщается после каждой. Так на документе в четыреста страниц человек
видит движение с первых секунд, а не ждёт молча десять минут.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .pdf.content import read_page
from .pdf.embed import FontError, FontFamily
from .pdf.layout import Block, analyse
from .pdf.overlay import Builder
from .pdf.reader import Document, EncryptedPDFError, PDFError, load
from .pdf.typeface import family_for, uncovered
from .translate.cache import Cache
from .translate.languages import RIGHT_TO_LEFT
from .translate.service import Service

# Сколько страниц разбирается до одного захода к переводчику. Больше пачка —
# меньше обращений и лучше кэш; меньше пачка — чаще обновляется прогресс.
PAGE_CHUNK = 12


@dataclass(slots=True)
class Progress:
    """Состояние работы, каким его видит интерфейс."""

    stage: str = "read"
    pages_done: int = 0
    pages_total: int = 0
    blocks_done: int = 0
    blocks_total: int = 0
    detected: str = ""

    @property
    def percent(self) -> int:
        if not self.pages_total:
            return 0
        # Разбор и перевод — примерно девять десятых работы, сборка — остаток.
        share = self.pages_done / self.pages_total
        if self.stage == "build":
            return min(99, 90 + int(share * 10))
        return min(90, int(share * 90))


@dataclass(slots=True)
class Result:
    """Что получилось."""

    data: bytes
    pages: int = 0
    blocks: int = 0
    characters: int = 0
    detected: str = ""
    untouched: int = 0
    warnings: list[str] = field(default_factory=list)


Report = Callable[[Progress], None]


class Cancelled(RuntimeError):
    """Задачу сняли из интерфейса."""


def translate_document(
    data: bytes,
    *,
    source: str = "auto",
    target: str = "ru",
    cache: Cache | None = None,
    service: Service | None = None,
    family: FontFamily | None = None,
    report: Report | None = None,
    should_stop: Callable[[], bool] | None = None,
    title: str = "",
) -> Result:
    """Перевести PDF, сохранив вёрстку.

    `should_stop` опрашивается между пачками страниц: длинный документ должно
    быть можно снять, не дожидаясь конца.
    """
    doc = load(data)
    pages = doc.pages()
    if not pages:
        raise PDFError("в файле нет ни одной страницы")

    own_cache = cache is None
    cache = cache or Cache(None)
    service = service or Service(cache)
    typeface = family or family_for(target)
    builder = Builder(doc, typeface, title=title)
    progress = Progress(stage="read", pages_total=len(pages))
    warnings: list[str] = []
    characters = 0
    blocks_count = 0
    untouched = 0

    def tick() -> None:
        if report:
            report(progress)

    tick()
    try:
        for start in range(0, len(pages), PAGE_CHUNK):
            if should_stop and should_stop():
                raise Cancelled("перевод отменён")
            chunk = pages[start : start + PAGE_CHUNK]
            prepared: list[tuple[object, list[Block]]] = []
            texts: list[str] = []
            for page in chunk:
                blocks = analyse(read_page(page))
                prepared.append((page, blocks))
                texts.extend(block.text for block in blocks if block.translatable)

            progress.stage = "translate"
            progress.blocks_total += len(texts)
            tick()

            translated = service.translate_all(
                texts,
                source,
                target,
                should_stop=should_stop,
            )
            progress.blocks_done += len(texts)
            progress.detected = service.detected

            cursor = 0
            for page, blocks in prepared:
                for block in blocks:
                    if not block.translatable:
                        untouched += 1
                        continue
                    block.translated = translated[cursor]
                    characters += len(block.text)
                    blocks_count += 1
                    cursor += 1
                builder.add_page(page, blocks, rtl=target in RIGHT_TO_LEFT)
                progress.pages_done += 1
            if translated:
                warnings.extend(_font_warnings(typeface, translated, warnings))
            progress.stage = "read"
            tick()

        progress.stage = "build"
        tick()
        out = builder.render()
    finally:
        if own_cache:
            cache.close()

    return Result(
        data=out,
        pages=len(pages),
        blocks=blocks_count,
        characters=characters,
        detected=service.detected,
        untouched=untouched,
        warnings=warnings,
    )


def _font_warnings(family: FontFamily, texts: list[str], already: list[str]) -> list[str]:
    """Предупредить один раз, если в шрифте не хватает знаков перевода."""
    if already:
        return []
    from .pdf.embed import load as load_font

    try:
        ttf = load_font(family.regular)
    except FontError:
        return []
    sample = " ".join(texts[:40])
    missing = uncovered(ttf, sample)
    if not missing:
        return []
    shown = "".join(sorted(missing)[:12])
    return [
        f"в шрифте {family.name} нет некоторых знаков перевода ({shown}) — "
        "они будут напечатаны пустыми квадратами. Поставьте шрифт с этими "
        "символами и переведите заново."
    ]


def inspect(data: bytes) -> dict:
    """Быстрый обзор файла: сколько страниц и есть ли что переводить."""
    doc = load(data)
    pages = doc.pages()
    sample = pages[: min(3, len(pages))]
    blocks: list[Block] = []
    for page in sample:
        blocks.extend(analyse(read_page(page)))
    text = " ".join(block.text for block in blocks if block.translatable)
    return {
        "pages": len(pages),
        "sample_characters": len(text),
        "has_text": bool(text.strip()),
    }


__all__ = [
    "Cancelled",
    "Document",
    "EncryptedPDFError",
    "PDFError",
    "Progress",
    "Result",
    "inspect",
    "translate_document",
]
