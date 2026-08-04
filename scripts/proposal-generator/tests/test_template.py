"""Сборка PDF целиком: что документ валиден и ничего не уезжает за поля."""

from __future__ import annotations

import re
import zlib
from datetime import date

import pytest

from proposals.models import VAT_ADDED, LineItem, Party, Proposal, Section
from proposals.sample import demo_logo, demo_proposal
from proposals.template import MARGIN, TEMPLATES, format_date, render

A4_WIDTH = 595.2755905511812


@pytest.fixture(scope="module")
def demo(family):
    data, warnings = render(demo_proposal(date(2026, 7, 28)), family=family)
    return data, warnings


def page_contents(data: bytes) -> list[str]:
    """Достать распакованные потоки страниц — по ним удобно проверять вёрстку."""
    streams = []
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        body = match.group(1)
        try:
            text = zlib.decompress(body).decode("latin-1")
        except (zlib.error, UnicodeDecodeError):
            continue
        if " Tf " in text or " re " in text:
            streams.append(text)
    return streams


def test_produces_a_valid_pdf(demo):
    data, warnings = demo
    assert data.startswith(b"%PDF-1.7")
    assert data.rstrip().endswith(b"%%EOF")
    assert warnings == []
    assert b"/Type /Catalog" in data
    assert b"/Encoding /Identity-H" in data


def test_pages_are_a4(demo):
    data, _ = demo
    assert data.count(b"/Type /Page\n") or b"/Type /Page " in data
    assert b"/MediaBox [0 0 595.2756 841.8898]" in data


def test_demo_needs_two_pages(demo):
    data, _ = demo
    count = int(re.search(rb"/Type /Pages /Kids \[(.*?)\] /Count (\d+)", data).group(2))
    assert count == 2


def test_title_and_author_are_set(demo):
    data, _ = demo
    assert b"/Title" in data and b"/Author" in data


def test_logos_are_embedded_once(demo):
    """Оба логотипа — картинки XObject, каждая ровно один раз."""
    data, _ = demo
    assert data.count(b"/Subtype /Image") == 4  # два логотипа + две маски прозрачности
    assert data.count(b"/SMask") == 2


def test_nothing_is_drawn_outside_the_margins(demo):
    """Самая частая беда самодельной вёрстки — текст, уехавший за край листа."""
    data, _ = demo
    for content in page_contents(data):
        for match in re.finditer(r"1 0 0 1 (-?[\d.]+) (-?[\d.]+) Tm", content):
            x = float(match.group(1))
            assert x >= MARGIN - 1, f"текст начинается левее поля: x={x}"
            assert x < A4_WIDTH - MARGIN, f"текст начинается правее поля: x={x}"


def test_font_is_subset_not_whole(demo, family):
    data, _ = demo
    whole = family.regular.stat().st_size
    assert len(data) < whole / 2, "в PDF уехал весь шрифт вместо подмножества"


def test_text_is_extractable(demo):
    """ToUnicode должен позволять копировать текст — иначе PDF нельзя искать."""
    data, _ = demo
    assert b"/ToUnicode" in data
    assert b"beginbfchar" not in data, "CMap обязан быть в сжатом потоке"


@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_every_template_renders(family, template):
    proposal = demo_proposal(date(2026, 7, 28))
    proposal.template = template
    data, warnings = render(proposal, family=family)
    assert data.startswith(b"%PDF")
    assert warnings == []


def test_unknown_template_falls_back(family):
    proposal = demo_proposal(date(2026, 7, 28))
    proposal.template = "космический"
    data, _ = render(proposal, family=family)
    assert data.startswith(b"%PDF")


def test_empty_proposal_still_renders(family):
    """Пустая форма не должна ронять генератор — только выдать пустой бланк."""
    data, warnings = render(Proposal(), family=family)
    assert data.startswith(b"%PDF")
    assert warnings == []


def test_long_proposal_paginates(family):
    proposal = demo_proposal(date(2026, 7, 28))
    proposal.items = [
        LineItem(title=f"Позиция {n}", description="Описание " * 12, quantity=n, price=1000)
        for n in range(1, 40)
    ]
    data, _ = render(proposal, family=family)
    count = int(re.search(rb"/Type /Pages /Kids \[(.*?)\] /Count (\d+)", data).group(2))
    assert count >= 4


def test_broken_logo_is_reported_but_does_not_stop_rendering(family):
    proposal = demo_proposal(date(2026, 7, 28))
    proposal.client.logo = b"\x89PNG\r\n\x1a\n" + "мусор".encode()
    data, warnings = render(proposal, family=family)
    assert data.startswith(b"%PDF")
    assert any("логотип заказчика" in warning for warning in warnings)


def test_missing_glyphs_are_reported(family):
    proposal = demo_proposal(date(2026, 7, 28))
    proposal.intro = "Иероглиф 漢 в тексте"
    _, warnings = render(proposal, family=family)
    assert any("не содержит символов" in warning for warning in warnings)


def test_accent_colour_reaches_the_page(family):
    proposal = demo_proposal(date(2026, 7, 28))
    proposal.accent = "#ff0000"
    data, _ = render(proposal, family=family)
    assert any("1 0 0 rg" in content for content in page_contents(data))


def test_broken_accent_falls_back_to_default(family):
    proposal = demo_proposal(date(2026, 7, 28))
    proposal.accent = "не цвет"
    data, warnings = render(proposal, family=family)
    assert data.startswith(b"%PDF") and warnings == []


def test_proposal_without_logos_renders(family):
    proposal = demo_proposal(date(2026, 7, 28))
    proposal.sender.logo = None
    proposal.client.logo = None
    data, _ = render(proposal, family=family)
    assert b"/Subtype /Image" not in data


def test_client_logo_lands_in_the_pdf(family):
    proposal = Proposal(
        sender=Party(name="Я"),
        client=Party(name="Клиент"),
        items=[LineItem(title="Работа", price=100)],
    )
    proposal.client.logo = demo_logo("К")
    data, warnings = render(proposal, family=family)
    assert warnings == []
    assert b"/Subtype /Image" in data


def test_vat_and_sections_appear(family):
    proposal = Proposal(
        sender=Party(name="Исполнитель"),
        client=Party(name="Заказчик"),
        items=[LineItem(title="Работа", price=1000)],
        sections=[Section(title="Что входит", body="Всё")],
        vat_mode=VAT_ADDED,
        vat_rate=20,
    )
    data, warnings = render(proposal, family=family)
    assert warnings == []
    assert data.startswith(b"%PDF")


def test_same_input_gives_same_bytes(family):
    """Повторный рендер обязан совпадать байт в байт — иначе не сравнить версии."""
    proposal = demo_proposal(date(2026, 7, 28))
    first, _ = render(proposal, family=family)
    second, _ = render(demo_proposal(date(2026, 7, 28)), family=family)
    assert first == second


def test_format_date_is_russian():
    assert format_date(date(2026, 7, 28)) == "28 июля 2026"
    assert format_date(date(2026, 1, 1)) == "1 января 2026"
