"""Сериализация объектов PDF и сборка файла."""

from __future__ import annotations

import re
import zlib

import pytest

from proposals.pdf.objects import (
    Name,
    PDFFile,
    Raw,
    Ref,
    Stream,
    TextString,
    fmt_number,
    serialize,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, b"1"), (1.0, b"1"), (0.5, b"0.5"), (1.23456, b"1.2346"), (-0.0, b"0"),
     (True, b"true"), (None, b"null")],
)
def test_scalars(value, expected):
    assert serialize(value) == expected


def test_fmt_number_never_uses_exponent():
    assert "e" not in fmt_number(0.0000001)


def test_name_escapes_specials():
    assert serialize(Name("Font")) == b"/Font"
    assert serialize(Name("a b")) == b"/a#20b"


def test_text_string_is_utf16():
    """Кириллица в свойствах документа должна пережить любой просмотрщик."""
    out = serialize(TextString("Привет"))
    assert out.startswith(b"<FEFF")
    assert bytes.fromhex(out[1:-1].decode())[2:].decode("utf-16-be") == "Привет"


def test_literal_string_escapes_parentheses():
    assert serialize(b"a(b)c\\") == rb"(a\(b\)c\\)"


def test_dict_and_array():
    out = serialize({"Type": Name("Page"), "Kids": [Ref(3), 4]})
    assert out == b"<</Type /Page /Kids [3 0 R 4]>>"


def test_raw_passes_through():
    assert serialize([Raw(b"<AB>")]) == b"[<AB>]"


def test_stream_compresses_and_reports_length():
    payload = b"x" * 5000
    out = Stream(payload).serialize()
    assert b"/Filter /FlateDecode" in out
    body = out.split(b"stream\n", 1)[1].rsplit(b"\nendstream", 1)[0]
    assert zlib.decompress(body) == payload
    assert f"/Length {len(body)}".encode() in out


def test_stream_skips_compression_when_it_does_not_help():
    out = Stream(b"ab").serialize()
    assert b"FlateDecode" not in out


def test_build_produces_valid_xref():
    pdf = PDFFile()
    page = pdf.add({"Type": Name("Page")})
    root = pdf.add({"Type": Name("Catalog"), "Pages": page})
    data = pdf.build(root, doc_id=b"\x01\x02")

    assert data.startswith(b"%PDF-1.7")
    assert data.rstrip().endswith(b"%%EOF")

    startxref = int(re.search(rb"startxref\n(\d+)", data).group(1))
    assert data[startxref : startxref + 4] == b"xref"

    # Каждое смещение из таблицы должно указывать на «N 0 obj».
    entries = re.findall(rb"^(\d{10}) 00000 n $", data[startxref:], re.M)
    for number, entry in enumerate(entries, start=1):
        assert data[int(entry) :].startswith(f"{number} 0 obj".encode())


def test_reserved_object_must_be_filled():
    pdf = PDFFile()
    ref = pdf.reserve()
    root = pdf.add({"Type": Name("Catalog")})
    with pytest.raises(ValueError, match="зарезервирован"):
        pdf.build(root)
    pdf.assign(ref, {"Type": Name("Pages")})
    assert pdf.build(root)


def test_unknown_type_is_rejected():
    with pytest.raises(TypeError):
        serialize(object())
