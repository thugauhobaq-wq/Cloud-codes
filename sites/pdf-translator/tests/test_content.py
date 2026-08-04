"""Исполнение контент-потока: координаты, матрицы, вложенные объекты."""

from __future__ import annotations

import pytest

from pdftr.pdf.content import IDENTITY, apply, multiply, read_page
from pdftr.pdf.reader import load


def runs_of(pdf: bytes, index: int = 0):
    return read_page(load(pdf).pages()[index]).runs


def test_matrix_multiplication_matches_the_spec():
    shift = (1, 0, 0, 1, 10, 20)
    scale = (2, 0, 0, 2, 0, 0)
    assert apply(multiply(shift, scale), 0, 0) == (20, 40)
    assert apply(IDENTITY, 3, 4) == (3, 4)


def test_text_position_and_size(simple_pdf):
    runs = runs_of(simple_pdf)
    first = runs[0]
    assert first.text == "Annual Report"
    assert first.x0 == pytest.approx(60.0)
    assert first.y0 == pytest.approx(780.0)
    assert first.size == pytest.approx(18.0)
    assert first.x1 > first.x0


def test_every_line_is_seen(simple_pdf):
    assert len(runs_of(simple_pdf)) == 5


def test_operator_ranges_point_at_real_bytes(simple_pdf):
    page = load(simple_pdf).pages()[0]
    content = page.content()
    for run in read_page(page).runs:
        assert run.op_start >= 0 and run.op_end > run.op_start
        assert b"Tj" in content[run.op_start : run.op_end]
        assert not run.in_form


def test_tj_array_offsets_shift_the_text(family, builder):
    doc = builder()
    hexed = doc.face.encode("AB").hex()
    tail = doc.face.encode("CD").hex()
    doc.page([f"BT /F1 10 Tf 1 0 0 1 50 700 Tm [<{hexed}> -500 <{tail}>] TJ ET"])
    runs = runs_of(doc.render())
    assert [run.text for run in runs] == ["AB", "CD"]
    # Сдвиг -500 в тысячных долях кегля — это пять пунктов вправо.
    assert runs[1].x0 == pytest.approx(runs[0].x1 + 5.0, abs=0.01)


def test_cm_scales_the_text(family, builder):
    doc = builder()
    doc.page([f"q 2 0 0 2 0 0 cm {doc.text(25, 350, 'Scaled', 10)} Q"])
    run = runs_of(doc.render())[0]
    assert run.size == pytest.approx(20.0)
    assert (run.x0, run.y0) == pytest.approx((50.0, 700.0))


def test_state_is_restored_after_q_and_Q(family, builder):
    doc = builder()
    doc.page(
        [
            "q 3 0 0 3 0 0 cm Q",  # масштаб не должен пережить Q
            doc.text(50, 700, "Plain", 10),
        ]
    )
    assert runs_of(doc.render())[0].size == pytest.approx(10.0)


def test_leading_and_star_operator(family, builder):
    doc = builder()
    first = doc.face.encode("First").hex()
    second = doc.face.encode("Second").hex()
    doc.page([f"BT /F1 10 Tf 14 TL 1 0 0 1 50 700 Tm <{first}> Tj T* <{second}> Tj ET"])
    runs = runs_of(doc.render())
    assert runs[1].y0 == pytest.approx(686.0)


def test_invisible_text_is_marked(family, builder):
    doc = builder()
    doc.page([f"BT /F1 10 Tf 3 Tr 1 0 0 1 50 700 Tm <{doc.face.encode('Hidden').hex()}> Tj ET"])
    run = runs_of(doc.render())[0]
    assert run.invisible
    assert run.restore_render == 3


def test_rotated_text_reports_its_angle(family, builder):
    doc = builder()
    doc.page([f"BT /F1 10 Tf 0 1 -1 0 300 300 Tm <{doc.face.encode('Turned').hex()}> Tj ET"])
    run = runs_of(doc.render())[0]
    assert abs(run.angle - 90.0) < 0.01


def test_filled_rectangle_is_remembered(family, builder):
    doc = builder()
    doc.page(["0.9 0.2 0.2 rg 100 100 200 50 re f", doc.text(110, 120, "On red")])
    content = read_page(load(doc.render()).pages()[0])
    assert len(content.rects) == 1
    rect = content.rects[0]
    assert (rect.x0, rect.y0, rect.x1, rect.y1) == pytest.approx((100, 100, 300, 150))
    assert rect.color == pytest.approx((0.9, 0.2, 0.2))
    assert rect.contains(150, 120)


def test_text_colour_is_tracked(family, builder):
    doc = builder()
    doc.page([f"BT 0.2 0.4 0.6 rg /F1 10 Tf 1 0 0 1 50 700 Tm "
              f"<{doc.face.encode('Coloured').hex()}> Tj ET"])
    assert runs_of(doc.render())[0].color == pytest.approx((0.2, 0.4, 0.6))


def test_gray_and_cmyk_colours(family, builder):
    doc = builder()
    doc.page([f"BT 0.5 g /F1 10 Tf 1 0 0 1 50 700 Tm <{doc.face.encode('Grey').hex()}> Tj ET"])
    assert runs_of(doc.render())[0].color == pytest.approx((0.5, 0.5, 0.5))


def test_inline_image_is_skipped(family, builder):
    doc = builder()
    # Между ID и EI лежат «пиксели», среди которых нарочно встречается «Tj».
    doc.page([
        "BI /W 2 /H 2 /BPC 8 /CS /G ID \x01Tj\x02\x03 EI",
        doc.text(50, 700, "After image"),
    ])
    runs = runs_of(doc.render())
    assert [run.text for run in runs] == ["After image"]
