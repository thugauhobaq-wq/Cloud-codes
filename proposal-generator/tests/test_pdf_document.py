"""Холст: цвета, переносы, страницы."""

from __future__ import annotations

import re
import zlib

import pytest

from proposals.pdf import BOLD, Document, mix, readable_on, rgb
from proposals.sample import demo_logo


@pytest.fixture
def doc(family) -> Document:
    return Document(family, title="Тест")


def content_of(data: bytes) -> str:
    """Первый распакованный поток с текстом — содержимое страницы."""
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        try:
            text = zlib.decompress(match.group(1)).decode("latin-1")
        except (zlib.error, UnicodeDecodeError):
            continue
        if " Tf " in text:
            return text
    return ""


# --- цвета ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [("#000", (0, 0, 0)), ("#ffffff", (1, 1, 1)), ("fff", (1, 1, 1))],
)
def test_rgb_parses(value, expected):
    assert rgb(value) == pytest.approx(expected)


@pytest.mark.parametrize("value", ["", "#12", "не цвет", "#gggggg"])
def test_rgb_rejects_nonsense(value):
    with pytest.raises(ValueError, match="непонятный цвет"):
        rgb(value)


def test_rgb_passes_tuples_through():
    assert rgb((0.1, 0.2, 0.3)) == (0.1, 0.2, 0.3)


def test_mix():
    assert mix((0, 0, 0), (1, 1, 1), 0.5) == pytest.approx((0.5, 0.5, 0.5))
    assert mix((0, 0, 0), (1, 1, 1), 0) == (0, 0, 0)


def test_readable_on_picks_contrast():
    """Акцент задаёт клиент: на жёлтом белый текст пропадает, нужен чёрный."""
    assert readable_on(rgb("#1f5eff")) == (1.0, 1.0, 1.0)
    assert readable_on(rgb("#f5c518")) == (0.0, 0.0, 0.0)


# --- текст ---------------------------------------------------------------------


def test_wrap_respects_width(doc):
    text = "Короткие слова которые надо разложить по строкам аккуратно и без спешки"
    for line in doc.wrap(text, 120, size=10):
        assert doc.measure(line, size=10) <= 120


def test_wrap_keeps_explicit_newlines(doc):
    assert doc.wrap("первая\nвторая", 400, size=10) == ["первая", "вторая"]


def test_wrap_breaks_words_longer_than_the_line(doc):
    """Ссылка или артикул без пробелов не должны вылезать за колонку."""
    lines = doc.wrap("а" * 200, 60, size=10)
    assert len(lines) > 1
    for line in lines:
        assert doc.measure(line, size=10) <= 60


def test_wrap_of_empty_text(doc):
    assert doc.wrap("", 100, size=10) == [""]


def test_ellipsize(doc):
    long_text = "Очень длинное название организации, которое никуда не влезет"
    short = doc.ellipsize(long_text, 100, size=10)
    assert short.endswith("…")
    assert doc.measure(short, size=10) <= 100
    assert doc.ellipsize("Коротко", 200, size=10) == "Коротко"


def test_measure_is_style_aware(doc):
    if doc.family.bold is None:
        pytest.skip("в системе нет жирного начертания")
    assert doc.measure("Тест", size=10, style=BOLD) > doc.measure("Тест", size=10)


def test_text_height_counts_lines(doc):
    single = doc.text_height("Строка", 400, size=10, leading=1.4)
    assert single == pytest.approx(14)
    assert doc.text_height("Строка\nВторая", 400, size=10, leading=1.4) == pytest.approx(28)


def test_check_glyphs_collects_missing(doc):
    doc.check_glyphs("Обычный текст")
    assert doc.missing_glyphs == set()
    doc.check_glyphs("Иероглиф 漢")
    assert "漢" in doc.missing_glyphs


# --- страницы и вывод ----------------------------------------------------------


def test_render_of_an_empty_document_has_one_page(doc):
    data = doc.render()
    assert b"/Count 1" in data


def test_text_returns_the_next_y(doc):
    page = doc.new_page()
    y = page.text(50, 100, "Строка", size=10)
    assert y > 100
    assert page.text(50, 100, "", size=10) == y, "пустая строка занимает ту же высоту"


def test_alignment_shifts_the_origin(doc):
    page = doc.new_page()
    page.text(50, 100, "Текст", size=10, align="right", width=200)
    positions = re.findall(r"1 0 0 1 ([\d.]+) [\d.]+ Tm", "\n".join(page._ops))
    assert float(positions[0]) > 50


def test_char_spacing_is_always_emitted(doc):
    """Tc живёт до конца потока: забыть обнулить — значит разогнать всю страницу."""
    page = doc.new_page()
    page.text(50, 50, "Разрядка", size=10, char_spacing=0.8)
    page.text(50, 70, "Обычный", size=10)
    spacings = re.findall(r"([\d.]+) Tc", "\n".join(page._ops))
    assert spacings == ["0.8", "0"]


def test_shapes_are_written(doc):
    page = doc.new_page()
    page.rect(10, 10, 100, 50, fill=(1, 0, 0))
    page.round_rect(10, 70, 100, 50, 8, stroke=(0, 0, 1))
    page.line(10, 130, 110, 130, dash=(2, 2))
    ops = "\n".join(page._ops)
    assert " re f" in ops and " c " in ops and "[2 2] 0 d" in ops


def test_rect_without_paint_draws_nothing(doc):
    page = doc.new_page()
    page.rect(0, 0, 10, 10)
    assert page._ops == []


def test_image_is_deduplicated(doc):
    page = doc.new_page()
    logo = doc.add_image(demo_logo("А"))
    same = doc.add_image(demo_logo("А"))
    assert logo.key == same.key

    page.image(logo, 10, 10, 50, 50)
    data = doc.render()
    assert data.count(b"/Subtype /Image") == 2  # картинка + маска прозрачности


def test_image_fit_keeps_aspect(doc):
    page = doc.new_page()
    logo = doc.add_image(demo_logo("А"))  # квадратный
    width, height = page.image_fit(logo, 0, 0, 200, 40)
    assert width == pytest.approx(height)
    assert height <= 40


def test_unused_images_are_not_listed_in_resources(doc):
    doc.add_image(demo_logo("А"))
    page = doc.new_page()
    page.text(50, 50, "Без картинок", size=10)
    data = doc.render()
    assert b"/XObject" not in data


def test_document_metadata_survives_cyrillic(doc):
    doc.new_page().text(50, 50, "Привет", size=10)
    data = doc.render()
    assert b"/Title <FEFF" in data


def test_content_stream_is_compressed(doc):
    page = doc.new_page()
    for index in range(80):
        page.text(50, 40 + index * 9, f"Строка номер {index}", size=8)
    data = doc.render()
    assert "Строка" not in content_of(data), "текст пишется глифами, а не символами"
    assert b"/Filter /FlateDecode" in data
