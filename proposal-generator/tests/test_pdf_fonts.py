"""Разбор TrueType и подмножество глифов."""

from __future__ import annotations

import struct

import pytest

from proposals.pdf.fonts import FontError, FontFace, TrueTypeFont, find_family, load
from proposals.pdf.objects import PDFFile


@pytest.fixture(scope="module")
def ttf(request):
    family = request.getfixturevalue("family")
    return load(family.regular)


def test_font_metrics_are_sane(ttf):
    assert ttf.units_per_em in (1000, 2048)
    assert ttf.num_glyphs > 100
    assert ttf.ascent > 0 > ttf.descent
    assert ttf.bbox[0] < ttf.bbox[2]


def test_cyrillic_is_present(ttf):
    for char in "АБВЯабвяЁё№":
        assert ttf.has(ord(char)), f"нет глифа для {char!r}"
    assert ttf.glyph_id(ord("А")) != ttf.glyph_id(ord("A")), "кириллица и латиница слиплись"


def test_missing_codepoint_maps_to_notdef(ttf):
    assert ttf.glyph_id(0x10FFFD) == 0


def test_advance_scales_to_thousandths(ttf):
    gid = ttf.glyph_id(ord("i"))
    wide = ttf.glyph_id(ord("W"))
    assert ttf.scale(ttf.advance(gid)) < ttf.scale(ttf.advance(wide))
    assert 0 < ttf.scale(ttf.advance(wide)) <= 2000


def test_subset_is_a_readable_font(ttf):
    used = {ttf.glyph_id(ord(char)) for char in "Привет, мир!"}
    subset = TrueTypeFont(ttf.subset(used), "<подмножество>")

    assert subset.num_glyphs == ttf.num_glyphs, "нумерация глифов должна сохраняться"
    assert subset.units_per_em == ttf.units_per_em
    for gid in used:
        assert subset.glyph_data(gid) == ttf.glyph_data(gid)


def test_subset_drops_unused_outlines(ttf):
    used = {ttf.glyph_id(ord(char)) for char in "Привет"}
    subset = ttf.subset(used)
    assert len(subset) < len(ttf.data) / 3, "подмножество должно быть заметно меньше"

    parsed = TrueTypeFont(subset, "<подмножество>")
    spare = next(
        gid for gid in range(1, ttf.num_glyphs)
        if gid not in used and ttf.glyph_data(gid)
    )
    assert parsed.glyph_data(spare) == b""


def test_subset_keeps_composite_parts(ttf):
    """Ё состоит из Е и диерезиса — части обязаны попасть в подмножество."""
    gid = ttf.glyph_id(ord("Ё"))
    parts = ttf.components(gid)
    if not parts:
        pytest.skip("в этом шрифте Ё нарисован цельным контуром")
    subset = TrueTypeFont(ttf.subset({gid}), "<подмножество>")
    for part in parts:
        assert subset.glyph_data(part) == ttf.glyph_data(part)


def test_subset_checksum_adjustment_is_recomputed(ttf):
    data = ttf.subset({ttf.glyph_id(ord("А"))})
    parsed = TrueTypeFont(data)
    start, _ = parsed.tables["head"]
    assert struct.unpack_from(">I", data, start + 8)[0] != 0


def test_rejects_non_truetype():
    with pytest.raises(FontError, match="не похоже"):
        TrueTypeFont(b"not a font at all really")


# --- FontFace: то, чем пользуется вёрстка -------------------------------------


def test_measure_grows_with_text(ttf):
    face = FontFace(key="F1", ttf=ttf, ps_name="Test")
    assert face.measure("", 10) == 0
    assert face.measure("ии", 10) == pytest.approx(face.measure("и", 10) * 2)
    assert face.measure("и", 20) == pytest.approx(face.measure("и", 10) * 2)


def test_encode_produces_two_bytes_per_glyph(ttf):
    face = FontFace(key="F1", ttf=ttf, ps_name="Test")
    encoded = face.encode("Аб")
    assert len(encoded) == 4
    assert struct.unpack(">H", encoded[:2])[0] == ttf.glyph_id(ord("А"))
    assert face.used == {ttf.glyph_id(ord("А")), ttf.glyph_id(ord("б"))}


def test_missing_reports_absent_characters(ttf):
    face = FontFace(key="F1", ttf=ttf, ps_name="Test")
    assert face.missing("Привет") == set()
    # Какой именно символ отсутствует, зависит от шрифта, — ищем первый попавшийся.
    absent = next(chr(code) for code in range(0x4E00, 0x4F00) if not ttf.has(code))
    assert face.missing(f"Привет {absent}") == {absent}


def test_widths_table_groups_runs(ttf):
    face = FontFace(key="F1", ttf=ttf, ps_name="Test")
    face.used = {10, 11, 12, 40}
    widths = face._widths()
    assert widths[0] == 10
    assert len(widths[1]) == 3, "подряд идущие глифы должны склеиться в один диапазон"
    assert widths[2] == 40


def test_to_unicode_maps_glyphs_back(ttf):
    face = FontFace(key="F1", ttf=ttf, ps_name="Test")
    face.encode("Ж")
    cmap = face._to_unicode().decode("ascii")
    assert "beginbfchar" in cmap
    assert f"<{ttf.glyph_id(ord('Ж')):04X}> <{ord('Ж'):04X}>" in cmap


def test_write_emits_composite_font(ttf):
    pdf = PDFFile()
    face = FontFace(key="F1", ttf=ttf, ps_name="TestFont")
    face.encode("Тест")
    ref = face.write(pdf)

    from proposals.pdf.objects import serialize

    dump = serialize(pdf._objects[ref.num - 1])
    assert b"/Subtype /Type0" in dump
    assert b"/Encoding /Identity-H" in dump
    assert b"/ToUnicode" in dump


# --- поиск шрифтов -------------------------------------------------------------


def test_find_family_by_name(family):
    assert find_family(family.name).regular == family.regular


def test_find_family_by_path(family):
    found = find_family(str(family.regular))
    assert found.regular == family.regular


def test_find_family_rejects_unknown_name():
    with pytest.raises(FontError, match="не найден"):
        find_family("Шрифт Которого Нет")


def test_path_for_falls_back_to_regular(family):
    assert family.path_for(bold=False, italic=False) == family.regular
    assert family.path_for(bold=True, italic=True) is not None
