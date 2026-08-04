"""Перевод документа целиком — от байтов до байтов."""

from __future__ import annotations

import pytest
from conftest import FakeTranslator, as_xref_stream

from pdftr.pdf.content import read_page
from pdftr.pdf.layout import analyse
from pdftr.pdf.reader import EncryptedPDFError, PDFError, load
from pdftr.pipeline import Cancelled, Progress, inspect, translate_document
from pdftr.translate.cache import Cache
from pdftr.translate.service import Service


def service(engine=None) -> Service:
    return Service(Cache(None), engine or FakeTranslator(), workers=1)


def texts_of(data: bytes) -> list[str]:
    doc = load(data)
    out = []
    for page in doc.pages():
        out.extend(block.text for block in analyse(read_page(page)))
    return out


def test_translates_and_stays_a_valid_pdf(simple_pdf, family):
    result = translate_document(simple_pdf, source="en", target="ru", service=service())
    assert result.data.startswith(b"%PDF")
    assert result.pages == 1
    assert result.blocks >= 3
    assert result.characters > 0
    assert load(result.data).pages()


def test_translation_appears_on_the_page(simple_pdf, family):
    result = translate_document(simple_pdf, source="en", target="ru", service=service())
    found = " ".join(texts_of(result.data))
    assert "»Annual Report" in found


def test_original_text_stops_being_visible(simple_pdf, family):
    result = translate_document(simple_pdf, source="en", target="ru", service=service())
    page = load(result.data).pages()[0]
    visible = [run.text for run in read_page(page).runs if not run.invisible]
    assert not any(text.startswith("Annual Report") for text in visible)
    hidden = [run.text for run in read_page(page).runs if run.invisible]
    assert any("Annual Report" in text for text in hidden)


def test_numbers_are_left_alone(simple_pdf, family):
    engine = FakeTranslator()
    translate_document(simple_pdf, source="en", target="ru", service=service(engine))
    assert not any(text.strip() == "page 1" and text.isdigit() for text in engine.texts)


def test_every_page_is_processed(family, builder):
    doc = builder()
    for index in range(5):
        doc.page([doc.text(60, 700, f"Page number {index}")])
    result = translate_document(doc.render(), source="en", target="ru", service=service())
    assert result.pages == 5
    assert len(load(result.data).pages()) == 5


def test_works_with_xref_stream_documents(simple_pdf, family):
    result = translate_document(
        as_xref_stream(simple_pdf), source="en", target="ru", service=service()
    )
    assert result.pages == 1
    assert "»Annual Report" in " ".join(texts_of(result.data))


def test_progress_is_reported_in_order(family, builder):
    doc = builder()
    for index in range(30):
        doc.page([doc.text(60, 700, f"Some page text {index}")])
    seen: list[Progress] = []
    translate_document(
        doc.render(),
        source="en",
        target="ru",
        service=service(),
        report=lambda progress: seen.append(
            Progress(progress.stage, progress.pages_done, progress.pages_total)
        ),
    )
    assert seen[0].pages_done == 0
    assert seen[-1].pages_done == 30
    assert [item.pages_done for item in seen] == sorted(item.pages_done for item in seen)
    assert {item.stage for item in seen} >= {"read", "translate", "build"}


def test_cancellation_stops_the_work(family, builder):
    doc = builder()
    for index in range(40):
        doc.page([doc.text(60, 700, f"Page {index} with text")])
    with pytest.raises(Cancelled):
        translate_document(
            doc.render(),
            source="en",
            target="ru",
            service=service(),
            should_stop=lambda: True,
        )


def test_encrypted_document_is_refused(simple_pdf):
    encrypted = simple_pdf.replace(b"/Root", b"/Encrypt 99 0 R /Root", 1)
    with pytest.raises(EncryptedPDFError):
        translate_document(encrypted, service=service())


def test_garbage_is_refused():
    with pytest.raises(PDFError):
        translate_document(b"not a pdf at all", service=service())


def test_document_without_text_survives(family, builder):
    doc = builder()
    doc.page(["0 0 0 rg 100 100 200 200 re f"])  # только прямоугольник
    result = translate_document(doc.render(), source="en", target="ru", service=service())
    assert result.blocks == 0
    assert load(result.data).pages()


def test_inspect_counts_pages(simple_pdf):
    found = inspect(simple_pdf)
    assert found["pages"] == 1
    assert found["has_text"]
    assert found["sample_characters"] > 20


def test_inspect_sees_a_page_without_text(family, builder):
    doc = builder()
    doc.page(["1 0 0 rg 10 10 100 100 re f"])
    assert inspect(doc.render())["has_text"] is False


def test_repeated_text_is_translated_once(family, builder):
    doc = builder()
    for _ in range(6):
        doc.page([doc.text(60, 700, "Exactly the same header")])
    engine = FakeTranslator()
    translate_document(doc.render(), source="en", target="ru", service=service(engine))
    assert engine.texts.count("Exactly the same header") == 1
