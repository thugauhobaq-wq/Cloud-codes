"""Сборка результата: копирование объектов, сокрытие оригинала, вёрстка перевода."""

from __future__ import annotations

import pytest

from pdftr.pdf.content import read_page
from pdftr.pdf.embed import FontFace
from pdftr.pdf.embed import load as load_font
from pdftr.pdf.layout import analyse
from pdftr.pdf.objects import PDFFile, Ref
from pdftr.pdf.overlay import (
    Builder,
    Copier,
    Faces,
    available,
    draw_block,
    fit,
    hide_originals,
    room_below,
    wrap,
)
from pdftr.pdf.reader import load


class Run:
    """Минимальный прогон: сокрытию нужны только границы оператора."""

    def __init__(self, start, end, restore=0, in_form=False):
        self.op_start, self.op_end = start, end
        self.restore_render, self.in_form = restore, in_form


# --- сокрытие оригинала ----------------------------------------------------


def test_hide_wraps_the_operator_in_invisible_mode():
    data = b"BT /F1 10 Tf 1 0 0 1 5 5 Tm (hi) Tj ET"
    start = data.index(b"(hi)")
    out = hide_originals(data, [Run(start, start + len(b"(hi) Tj"))])
    assert b"3 Tr(hi) Tj 0 Tr" in out
    assert b"(hi) Tj" in out  # сам оператор остался — сдвиг матрицы сохраняется


def test_hide_restores_the_previous_render_mode():
    data = b"(x) Tj"
    out = hide_originals(data, [Run(0, len(data), restore=2)])
    assert out.endswith(b" 2 Tr")


def test_form_runs_are_not_touched():
    data = b"(x) Tj"
    assert hide_originals(data, [Run(0, len(data), in_form=True)]) == data


def test_overlapping_ranges_are_skipped():
    data = b"(one) Tj (two) Tj"
    out = hide_originals(data, [Run(0, 12), Run(5, 17)])
    assert out.count(b"3 Tr") == 1  # второй, налезающий на первый, пропущен


def test_nothing_to_hide_returns_the_same_bytes():
    assert hide_originals(b"unchanged", []) == b"unchanged"


# --- перенос и подбор кегля -----------------------------------------------


@pytest.fixture()
def face(family):
    return FontFace(
        key="T", ttf=load_font(family.path_for(False, False)), ps_name="AAAAAA+T"
    )


def test_wrap_breaks_on_width(face):
    lines = wrap(face, "one two three four five six seven eight", 60.0, 10.0)
    assert len(lines) > 1
    assert all(face.measure(line, 10.0) <= 60.5 for line in lines)


def test_wrap_splits_a_word_that_does_not_fit(face):
    lines = wrap(face, "Donaudampfschifffahrtsgesellschaft", 40.0, 10.0)
    assert len(lines) > 1
    assert "".join(lines) == "Donaudampfschifffahrtsgesellschaft"


def test_wrap_keeps_short_text_on_one_line(face):
    assert wrap(face, "short", 300.0, 10.0) == ["short"]


def test_fit_shrinks_until_the_text_fits(simple_pdf, family):
    blocks = analyse(read_page(load(simple_pdf).pages()[0]))
    block = next(item for item in blocks if len(item.lines) == 3)
    faces, pdf = Faces(family), PDFFile()
    long_text = block.text * 3
    plan = fit(faces, pdf, block, long_text, room_below(block, blocks))
    assert plan.size < block.size
    assert plan.size >= 4.0


def test_fit_keeps_the_original_size_when_it_can(simple_pdf, family):
    blocks = analyse(read_page(load(simple_pdf).pages()[0]))
    block = next(item for item in blocks if len(item.lines) == 3)
    faces, pdf = Faces(family), PDFFile()
    plan = fit(faces, pdf, block, "Коротко", room_below(block, blocks))
    assert plan.size == pytest.approx(block.size)
    assert plan.fits


# --- отведённое место ------------------------------------------------------


def test_single_line_may_grow_to_the_right(simple_pdf):
    blocks = analyse(read_page(load(simple_pdf).pages()[0]))
    footer = next(item for item in blocks if item.text == "page 1")
    x0, width = available(footer, blocks)
    assert x0 == pytest.approx(footer.x0)
    assert width > footer.width * 2  # справа пусто — есть куда расти


def test_multiline_block_keeps_its_column(simple_pdf):
    blocks = analyse(read_page(load(simple_pdf).pages()[0]))
    body = next(item for item in blocks if len(item.lines) == 3)
    assert available(body, blocks) == (body.x0, body.width)


def test_columns_do_not_grow_into_each_other(two_columns_pdf):
    blocks = analyse(read_page(load(two_columns_pdf).pages()[0]))
    left = next(item for item in blocks if "Left column" in item.text)
    right = next(item for item in blocks if "Right column" in item.text)
    x0, width = available(left, blocks)
    assert x0 + width <= right.x0 + 1


def test_room_below_stops_at_the_next_block(simple_pdf):
    blocks = analyse(read_page(load(simple_pdf).pages()[0]))
    heading = blocks[0]
    assert room_below(heading, blocks) >= heading.height
    assert room_below(heading, blocks) < heading.height + heading.size * 3


# --- отрисовка -------------------------------------------------------------


def test_draw_block_writes_text_operators(simple_pdf, family):
    blocks = analyse(read_page(load(simple_pdf).pages()[0]))
    block = blocks[0]
    faces, pdf = Faces(family), PDFFile()
    plan = fit(faces, pdf, block, "Годовой отчёт", room_below(block, blocks))
    body = draw_block(block, plan, rtl=False)
    assert body.startswith("BT")
    assert "Tj" in body and "Tm" in body and body.endswith("ET")
    assert "re f" not in body  # белого фона нет — оригинал спрятан иначе


def test_block_on_a_plate_gets_a_patch(family, builder):
    doc = builder()
    doc.page(["0.1 0.2 0.7 rg 40 680 300 40 re f", doc.text(50, 695, "On a plate")])
    blocks = analyse(read_page(load(doc.render()).pages()[0]))
    faces, pdf = Faces(family), PDFFile()
    plan = fit(faces, pdf, blocks[0], "На плашке", room_below(blocks[0], blocks))
    body = draw_block(blocks[0], plan)
    assert "re f" in body
    assert "0.1 0.2 0.7 rg" in body


# --- копирование объектов --------------------------------------------------


def test_copier_renumbers_and_keeps_structure(simple_pdf):
    source = load(simple_pdf)
    pdf = PDFFile()
    copier = Copier(source, pdf)
    root = copier.convert(source.trailer["Root"])
    assert isinstance(root, Ref)
    built = pdf.build(root)
    again = load(built)
    assert len(again.pages()) == 1
    assert again.pages()[0].content() == source.pages()[0].content()


def test_copier_visits_each_object_once(simple_pdf):
    source = load(simple_pdf)
    copier = Copier(source, PDFFile())
    first = copier.ref_for(1)
    assert copier.ref_for(1) == first


def test_builder_leaves_untranslated_pages_alone(simple_pdf, family):
    source = load(simple_pdf)
    builder = Builder(source, family)
    page = source.pages()[0]
    builder.add_page(page, analyse(read_page(page)))
    out = load(builder.render())
    assert out.pages()[0].content() == page.content()
    assert builder.replaced == 0


def test_builder_replaces_translated_blocks(simple_pdf, family):
    source = load(simple_pdf)
    page = source.pages()[0]
    blocks = analyse(read_page(page))
    for block in blocks:
        if block.translatable:
            block.translated = "Переведено"
    builder = Builder(source, family)
    builder.add_page(page, blocks)
    data = builder.render()

    out = load(data)
    content = out.pages()[0].content()
    assert b"3 Tr" in content  # оригинал спрятан
    assert content.count(b"BT") > page.content().count(b"BT")  # перевод дописан
    assert builder.replaced == len([b for b in blocks if b.translatable])
    assert data.startswith(b"%PDF")


def test_builder_adds_its_font_to_page_resources(simple_pdf, family):
    source = load(simple_pdf)
    page = source.pages()[0]
    blocks = analyse(read_page(page))
    for block in blocks:
        block.translated = "Перевод"
    builder = Builder(source, family)
    builder.add_page(page, blocks)
    out = load(builder.render())
    fonts = out.resolve(out.pages()[0].resources.get("Font"))
    assert any(key.startswith("PdfTr") for key in fonts)
    assert "F1" in fonts  # исходный шрифт на месте, его текст ещё нужен
