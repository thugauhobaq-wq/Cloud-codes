"""Сборка строк, абзацев и колонок из текстовых прогонов."""

from __future__ import annotations

import pytest

from pdftr.pdf.content import PageContent, TextRun, read_page
from pdftr.pdf.fonts import Font
from pdftr.pdf.layout import analyse, build_blocks, build_lines, regions
from pdftr.pdf.reader import load

FONT = Font(key="F1", reliable=True)


def run(text, x, y, size=10.0, width=None, **kwargs):
    span = width if width is not None else len(text) * size * 0.5
    return TextRun(
        text=text, x0=x, y0=y, x1=x + span, size=size, font=kwargs.pop("font", FONT),
        color=kwargs.pop("color", (0.0, 0.0, 0.0)), space_width=size * 0.28, **kwargs
    )


def blocks_of(pdf: bytes, index: int = 0):
    return analyse(read_page(load(pdf).pages()[index]))


# --- строки ----------------------------------------------------------------


def test_runs_on_one_baseline_become_one_line():
    lines = build_lines([run("Hello", 50, 700), run("world", 90, 700)])
    assert len(lines) == 1
    assert lines[0].text == "Hello world"  # пробел восстановлен по промежутку


def test_touching_runs_are_not_split_by_a_space():
    first = run("Hel", 50, 700)
    second = run("lo", first.x1, 700)
    assert build_lines([first, second])[0].text == "Hello"


def test_different_baselines_are_different_lines():
    lines = build_lines([run("first", 50, 700), run("second", 50, 686)])
    assert [line.text for line in lines] == ["first", "second"]


def test_rotated_and_empty_runs_are_dropped():
    lines = build_lines([run("sideways", 50, 700, angle=90.0), run("   ", 50, 686)])
    assert lines == []


# --- абзацы ----------------------------------------------------------------


def test_consecutive_lines_form_one_paragraph():
    lines = build_lines([run("line one here", 50, 700 - index * 12) for index in range(4)])
    blocks = build_blocks(lines)
    assert len(blocks) == 1
    assert blocks[0].text.count("line one here") == 4


def test_a_gap_starts_a_new_paragraph():
    lines = build_lines(
        [run("first paragraph", 50, 700), run("first again", 50, 688),
         run("second paragraph", 50, 640)]
    )
    assert len(build_blocks(lines)) == 2


def test_a_different_size_starts_a_new_paragraph():
    lines = build_lines([run("Heading", 50, 700, size=18), run("body text", 50, 686)])
    assert len(build_blocks(lines)) == 2


def test_bullets_are_separate_paragraphs():
    lines = build_lines(
        [run("• first item", 50, 700), run("• second item", 50, 688),
         run("• third item", 50, 676)]
    )
    assert len(build_blocks(lines)) == 3


def test_hyphenated_words_are_joined_back():
    lines = build_lines([run("indepen-", 50, 700), run("dent service", 50, 688)])
    assert build_blocks(lines)[0].text.startswith("independent service")


def test_alignment_is_recognised():
    justified = build_blocks(
        build_lines([run("x", 50, 700 - i * 12, width=200) for i in range(4)])
    )[0]
    assert justified.align == "justify"

    centred = build_blocks(
        build_lines(
            [run("x", 50 + i * 5, 700 - i * 12, width=200 - i * 10) for i in range(3)]
        )
    )[0]
    assert centred.align == "center"


def test_untranslatable_blocks_are_marked():
    blocks = build_blocks(build_lines([run("12.5", 50, 700)]))
    assert not blocks[0].translatable


def test_unreliable_font_blocks_are_marked():
    broken = Font(key="F2", reliable=False)
    blocks = build_blocks(build_lines([run("Some text", 50, 700, font=broken)]))
    assert not blocks[0].translatable


# --- колонки ---------------------------------------------------------------


def test_columns_are_not_merged(two_columns_pdf):
    blocks = blocks_of(two_columns_pdf)
    texts = [block.text for block in blocks]
    assert any("Left column" in text and "Right column" not in text for text in texts)
    assert any("Right column" in text and "Left column" not in text for text in texts)


def test_heading_stays_whole_above_columns(two_columns_pdf):
    blocks = blocks_of(two_columns_pdf)
    assert blocks[0].text == "Wide heading across the page"


def test_staggered_columns_are_split():
    """Колонки, сдвинутые по вертикали, тоже должны разъехаться."""
    left = [run("left text here", 50, 700 - index * 12) for index in range(8)]
    right = [run("right text here", 320, 640 - index * 12) for index in range(8)]
    parts = regions(left + right)
    assert len(parts) >= 2
    for part in parts:
        xs = {round(item.x0) for item in part}
        assert not (50 in xs and 320 in xs)


def test_single_column_page_is_not_cut_into_pieces(simple_pdf):
    blocks = blocks_of(simple_pdf)
    body = [block for block in blocks if block.text.startswith("The system")]
    assert len(body) == 1
    assert len(body[0].lines) == 3


# --- фон -------------------------------------------------------------------


def test_background_under_a_paragraph_is_found(family, builder):
    doc = builder()
    doc.page(["0.1 0.2 0.7 rg 40 680 300 40 re f", doc.text(50, 695, "On a blue plate")])
    block = blocks_of(doc.render())[0]
    assert block.background == pytest.approx((0.1, 0.2, 0.7))


def test_white_background_is_ignored(family, builder):
    doc = builder()
    doc.page(["1 1 1 rg 40 680 300 40 re f", doc.text(50, 695, "On white")])
    assert blocks_of(doc.render())[0].background is None


def test_boxes_cover_each_line_separately(simple_pdf):
    block = next(item for item in blocks_of(simple_pdf) if len(item.lines) == 3)
    boxes = block.boxes()
    assert len(boxes) == 3
    for (x0, y0, x1, y1), line in zip(boxes, block.lines, strict=True):
        assert x0 <= line.x0 and x1 >= line.x1
        assert y0 <= line.bottom and y1 >= line.top


def test_empty_page_gives_no_blocks():
    assert analyse(PageContent()) == []
