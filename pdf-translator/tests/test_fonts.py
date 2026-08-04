"""Шрифты чужого документа: кодировки, ширины, ToUnicode."""

from __future__ import annotations

from pdftr.pdf.fonts import Font, glyph_to_char, load_font, page_fonts, parse_cmap
from pdftr.pdf.objects import Name
from pdftr.pdf.reader import Document, StreamObject, load


def test_glyph_names():
    assert glyph_to_char("A") == "A"
    assert glyph_to_char("space") == " "
    assert glyph_to_char("Aacute") == "Á"
    assert glyph_to_char("germandbls") == "ß"
    assert glyph_to_char("uni0416") == "Ж"
    assert glyph_to_char("u00E9") == "é"
    assert glyph_to_char("fi") == "fi"
    assert glyph_to_char("one.oldstyle") == "1"  # вариант глифа — берём основу
    assert glyph_to_char("g42") == ""  # номер глифа ни о чём не говорит


def test_cmap_bfchar_and_bfrange():
    source = b"""
    /CIDInit /ProcSet findresource begin
    1 begincodespacerange <0000> <FFFF> endcodespacerange
    2 beginbfchar <0003> <0020> <0024> <0041> endbfchar
    1 beginbfrange <0025> <0027> <0042> endbfrange
    endcmap
    """
    mapping, spaces = parse_cmap(source)
    assert mapping[0x03] == " "
    assert mapping[0x24] == "A"
    assert mapping[0x25] == "B"
    assert mapping[0x27] == "D"
    assert spaces == [(0, 0xFFFF, 2)]


def test_cmap_bfrange_with_array():
    mapping, _ = parse_cmap(b"1 beginbfrange <01> <03> [<0041> <0042> <0043>] endbfrange")
    assert [mapping[code] for code in (1, 2, 3)] == ["A", "B", "C"]


def test_cmap_ignores_broken_ranges():
    # Конец диапазона меньше начала — такой кусок просто пропускается.
    mapping, _ = parse_cmap(b"1 beginbfrange <05> <01> <0041> endbfrange")
    assert mapping == {}


def test_simple_font_uses_widths_and_encoding():
    doc = Document(b"%PDF-1.4\ntrailer << /Root 1 0 R >>\n1 0 obj << /Type /Catalog >> endobj")
    info = {
        "Subtype": Name("Type1"),
        "BaseFont": Name("Helvetica"),
        "FirstChar": 65,
        "Widths": [600, 700, 800],
        "Encoding": Name("WinAnsiEncoding"),
    }
    font = load_font(doc, "F1", info)
    assert font.char(65) == "A"
    assert font.width(65) == 600
    assert font.width(67) == 800
    assert font.width(200) == font.default_width
    assert font.reliable


def test_differences_override_the_base_encoding():
    doc = Document(b"%PDF-1.4\ntrailer << /Root 1 0 R >>\n1 0 obj << /Type /Catalog >> endobj")
    info = {
        "Subtype": Name("Type1"),
        "BaseFont": Name("Custom"),
        "Encoding": {"BaseEncoding": Name("WinAnsiEncoding"),
                     "Differences": [65, Name("bullet"), Name("uni0416")]},
    }
    font = load_font(doc, "F1", info)
    assert font.char(65) == "•"
    assert font.char(66) == "Ж"
    assert font.char(67) == "C"  # не тронуто — из базовой кодировки


def test_type0_font_is_two_byte_and_uses_w_array():
    doc = Document(b"%PDF-1.4\ntrailer << /Root 1 0 R >>\n1 0 obj << /Type /Catalog >> endobj")
    cmap = StreamObject(
        {}, b"1 begincodespacerange <0000> <FFFF> endcodespacerange "
            b"1 beginbfchar <0024> <0041> endbfchar", doc
    )
    info = {
        "Subtype": Name("Type0"),
        "BaseFont": Name("ABCDEF+Noto"),
        "Encoding": Name("Identity-H"),
        "ToUnicode": cmap,
        "DescendantFonts": [{"DW": 1000, "W": [0x24, [500], 0x30, 0x32, 750]}],
    }
    font = load_font(doc, "F1", info)
    assert font.two_byte
    assert font.base_font == "Noto"  # префикс подмножества снят
    assert font.codes(b"\x00\x24") == [0x24]
    assert font.char(0x24) == "A"
    assert font.width(0x24) == 500
    assert font.width(0x31) == 750
    assert font.width(0x99) == 1000


def test_font_without_anything_is_unreliable():
    doc = Document(b"%PDF-1.4\ntrailer << /Root 1 0 R >>\n1 0 obj << /Type /Catalog >> endobj")
    font = load_font(doc, "F1", {"Subtype": Name("Type0"), "DescendantFonts": []})
    assert not font.reliable


def test_type3_font_is_left_alone():
    doc = Document(b"%PDF-1.4\ntrailer << /Root 1 0 R >>\n1 0 obj << /Type /Catalog >> endobj")
    font = load_font(doc, "F1", {"Subtype": Name("Type3"), "FirstChar": 0, "Widths": [10]})
    assert not font.reliable


def test_codes_respects_codespace_widths():
    """Смешанная кодировка: младшие коды однобайтовые, старшие — двухбайтовые."""
    font = Font(key="F1", two_byte=True, codespaces=[(0, 0x7F, 1), (0x8000, 0xFFFF, 2)])
    assert font.codes(b"\x41\x80\x01") == [0x41, 0x8001]


def test_page_fonts_reads_resources(simple_pdf):
    doc = load(simple_pdf)
    fonts = page_fonts(doc, doc.pages()[0].resources)
    assert "F1" in fonts
    font = fonts["F1"]
    assert font.two_byte and font.reliable
    assert "".join(char for _c, char, _w in font.decode(b"\x00\x24")) != ""


def test_decode_round_trip(simple_pdf):
    """Текст, записанный нашим же кодировщиком, читается обратно."""
    doc = load(simple_pdf)
    page = doc.pages()[0]
    font = page_fonts(doc, page.resources)["F1"]
    content = page.content()
    start = content.index(b"<") + 1
    end = content.index(b">", start)
    raw = bytes.fromhex(content[start:end].decode("ascii"))
    assert "".join(char for _c, char, _w in font.decode(raw)) == "Annual Report"
