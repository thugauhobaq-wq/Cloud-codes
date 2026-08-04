"""Чтение PDF: синтаксис, таблицы объектов, дерево страниц."""

from __future__ import annotations

import zlib

import pytest
from conftest import as_xref_stream

from pdftr.pdf.objects import Name, Ref
from pdftr.pdf.reader import (
    Document,
    EncryptedPDFError,
    ObjectParser,
    PDFError,
    StreamObject,
    load,
)


def parse(source: bytes) -> object:
    return ObjectParser(source, 0).parse()


# --- синтаксис ------------------------------------------------------------


def test_numbers_and_keywords():
    assert parse(b"42") == 42
    assert parse(b"-3.5") == -3.5
    assert parse(b".5") == 0.5
    assert parse(b"true") is True
    assert parse(b"null") is None


def test_broken_numbers_do_not_crash():
    # В природе встречается и такое; главное — не упасть на разборе страницы.
    assert parse(b"--5") == -5
    assert parse(b"4.-5") == 4.0


def test_names_with_escapes():
    assert parse(b"/Name#20with#20spaces") == Name("Name with spaces")
    assert parse(b"/Simple") == Name("Simple")


def test_literal_strings():
    assert parse(rb"(hello)") == b"hello"
    assert parse(rb"(nested (parens) work)") == b"nested (parens) work"
    assert parse(rb"(tab\there)") == b"tab\there"
    assert parse(rb"(\101\102)") == b"AB"
    assert parse(rb"(line\
continued)") == b"linecontinued"


def test_hex_strings():
    assert parse(b"<48656C6C6F>") == b"Hello"
    assert parse(b"<4>") == b"@"  # недостающая цифра — ноль


def test_arrays_and_dicts():
    assert parse(b"[1 2 3]") == [1, 2, 3]
    assert parse(b"<< /A 1 /B (two) >>") == {"A": 1, "B": b"two"}
    assert parse(b"[1 0 R 2 0 R]") == [Ref(1, 0), Ref(2, 0)]


def test_reference_versus_two_numbers():
    assert parse(b"12 0 R") == Ref(12, 0)
    assert parse(b"[12 0]") == [12, 0]


def test_stream_uses_length():
    source = b"<< /Length 5 >>\nstream\nHELLO\nendstream"
    obj = ObjectParser(source, 0).parse()
    assert isinstance(obj, StreamObject)
    assert obj.raw == b"HELLO"


def test_stream_survives_wrong_length():
    # /Length соврала — конец потока ищется по слову endstream.
    source = b"<< /Length 999 >>\nstream\nHELLO\nendstream"
    obj = ObjectParser(source, 0).parse()
    assert obj.raw == b"HELLO"


# --- документ целиком ------------------------------------------------------


def test_reads_classic_xref(simple_pdf):
    doc = load(simple_pdf)
    assert len(doc.pages()) == 1
    assert doc.trailer.get("Root") is not None


def test_reads_xref_stream(simple_pdf):
    doc = load(as_xref_stream(simple_pdf))
    pages = doc.pages()
    assert len(pages) == 1
    assert b"Tj" in pages[0].content()


def test_reads_objects_from_object_stream(family, builder):
    """Объекты, упакованные в ObjStm, должны находиться наравне с обычными."""
    doc = builder()
    doc.page([doc.text(50, 700, "Inside object stream")])
    raw = doc.render()
    source = load(raw)
    page = source.pages()[0]

    packed = {key: value for key, value in page.info.items() if key != "Parent"}
    from pdftr.pdf.objects import serialize

    body = serialize(packed)
    header = b"9000 0 "
    stream_data = header + body
    payload = zlib.compress(stream_data)
    extra = (
        b"9001 0 obj\n<< /Type /ObjStm /N 1 /First "
        + str(len(header)).encode()
        + b" /Filter /FlateDecode /Length "
        + str(len(payload)).encode()
        + b" >>\nstream\n"
        + payload
        + b"\nendstream\nendobj\n"
    )
    # Дописываем в конец: xref сломается, ридер перейдёт на полный обход файла.
    doc_bytes = raw + extra
    found = Document(doc_bytes)
    assert isinstance(found.get(9001), StreamObject)
    inner = found._load_objstm(9001)
    assert inner[9000]["Type"] == Name("Page")


def test_recovers_from_broken_xref(simple_pdf):
    broken = simple_pdf.replace(b"startxref", b"startxrXf", 1)
    doc = load(broken)
    assert len(doc.pages()) == 1


def test_recovers_when_offsets_are_shifted(simple_pdf):
    # Кто-то дописал байты в начало файла — все смещения уехали.
    doc = load(b"%\xd0\xbc\xd1\x83\xd1\x81\xd0\xbe\xd1\x80\n" + simple_pdf)
    assert len(doc.pages()) == 1


def test_encrypted_file_is_refused(simple_pdf):
    encrypted = simple_pdf.replace(b"/Root", b"/Encrypt 99 0 R /Root", 1)
    with pytest.raises(EncryptedPDFError):
        load(encrypted)


def test_not_a_pdf():
    with pytest.raises(PDFError):
        load(b"just some text")
    with pytest.raises(PDFError):
        load(b"")


def test_page_inherits_from_the_tree(family, builder):
    doc = builder()
    doc.page([doc.text(50, 700, "inherited")])
    raw = doc.render()
    source = load(raw)
    page = source.pages()[0]
    assert page.width == pytest.approx(595.0)
    assert "Font" in page.resources


def test_page_box_and_rotation(family, builder):
    doc = builder(width=300, height=400)
    doc.page([doc.text(10, 10, "small")])
    page = load(doc.render()).pages()[0]
    assert (page.width, page.height) == (300.0, 400.0)
    assert page.rotation == 0


def test_multiple_pages_keep_their_order(family, builder):
    doc = builder()
    for index in range(4):
        doc.page([doc.text(50, 700, f"page {index}")])
    pages = load(doc.render()).pages()
    assert [page.index for page in pages] == [0, 1, 2, 3]
    assert all(page.ref is not None for page in pages)
