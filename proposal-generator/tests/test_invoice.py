"""Счёт на оплату."""

from __future__ import annotations

import re
from datetime import date

import pytest

from proposals import invoice
from proposals.models import VAT_ADDED, VAT_INCLUDED, Bank, LineItem, Party, Proposal
from proposals.sample import demo_proposal
from proposals.template import MARGIN

A4_WIDTH = 595.2755905511812


def title_of(data: bytes) -> str:
    """Заголовок документа: UTF-16BE в шестнадцатеричной строке.

    Текст страниц лежит в сжатых потоках и записан номерами глифов, так что
    проверять содержимое проще всего через свойства файла.
    """
    match = re.search(rb"/Title <([0-9A-F]+)>", data)
    assert match, "у счёта должен быть заголовок"
    return bytes.fromhex(match.group(1).decode())[2:].decode("utf-16-be")


def with_requisites(proposal: Proposal) -> Proposal:
    proposal.sender.inn = "771234567890"
    proposal.sender.bank = Bank(
        name="АО «Т-Банк», г. Москва",
        bik="044525974",
        account="40802810100000001234",
        corr_account="30101810145250000974",
    )
    proposal.client.inn = "1655123456"
    proposal.client.kpp = "165501001"
    return proposal


@pytest.fixture(scope="module")
def bill(family):
    return invoice.render(with_requisites(demo_proposal(date(2026, 7, 28))), family=family)


def text_of(data: bytes) -> str:
    """Текст документа через ToUnicode нам недоступен — берём из потоков."""
    import zlib

    chunks = []
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        try:
            chunks.append(zlib.decompress(match.group(1)).decode("latin-1"))
        except (zlib.error, UnicodeDecodeError):
            continue
    return "\n".join(chunks)


def test_renders_a_valid_pdf(bill):
    data, warnings = bill
    assert data.startswith(b"%PDF-1.7")
    assert data.rstrip().endswith(b"%%EOF")
    assert warnings == []


def test_title_names_the_document(bill):
    data, _ = bill
    assert b"/Title" in data
    assert b"/MediaBox [0 0 595.2756 841.8898]" in data


def test_fits_one_page(bill):
    data, _ = bill
    count = int(re.search(rb"/Type /Pages /Kids \[(.*?)\] /Count (\d+)", data).group(2))
    assert count == 1, "счёт на пять позиций обязан помещаться на лист"


def test_nothing_leaves_the_margins(bill):
    """Номер счёта — двадцать цифр, и он легко вылезал за край."""
    data, _ = bill
    for match in re.finditer(r"1 0 0 1 (-?[\d.]+) (-?[\d.]+) Tm", text_of(data)):
        x = float(match.group(1))
        assert MARGIN - 1 <= x < A4_WIDTH - MARGIN


def test_missing_bank_details_are_reported(family):
    proposal = Proposal(
        sender=Party(name="ИП Иванов"),
        client=Party(name="ООО «Рога»"),
        items=[LineItem(title="Консультация", price=5000)],
    )
    data, warnings = invoice.render(proposal, family=family)

    assert data.startswith(b"%PDF"), "счёт всё равно печатаем — вдруг реквизиты впишут руками"
    assert any("банковские реквизиты" in warning for warning in warnings)
    assert any("ИНН" in warning for warning in warnings)


def test_number_and_date_default_to_the_proposal(family):
    proposal = with_requisites(demo_proposal(date(2026, 7, 28)))
    data, _ = invoice.render(proposal, family=family)
    assert title_of(data) == "Счёт на оплату № 47 от 28 июля 2026"


def test_number_and_date_can_be_overridden(family):
    """Счёт нередко нумеруют своей серией, отдельной от предложений."""
    proposal = with_requisites(demo_proposal(date(2026, 7, 28)))
    data, _ = invoice.render(
        proposal, number="СЧ-100", issued_at=date(2026, 8, 1), family=family
    )
    assert title_of(data) == "Счёт на оплату № СЧ-100 от 1 августа 2026"


def test_totals_match_the_proposal(family, tmp_path):
    """Счёт и КП обязаны показывать одну и ту же сумму.

    Читалка PDF в зависимости проекта не входит: без неё тест пропускается, а
    остальные проверки счёта работают на голой стандартной библиотеке.
    """
    pdfium = pytest.importorskip("pypdfium2", reason="нужна читалка PDF")

    proposal = with_requisites(demo_proposal(date(2026, 7, 28)))
    data, _ = invoice.render(proposal, family=family)
    path = tmp_path / "schet.pdf"
    path.write_bytes(data)

    page = pdfium.PdfDocument(str(path)).get_page(0)
    # Разряды разделены неразрывным пробелом — сравниваем в одном виде.
    text = page.get_textpage().get_text_range().replace("\xa0", " ")

    from proposals.money import amount_in_words, format_amount

    total = proposal.totals().total
    assert format_amount(total, "RUB").replace("\xa0", " ") in text
    assert amount_in_words(total, "RUB") in text
    assert "Всего к оплате" in text


@pytest.mark.parametrize("vat_mode", ["none", VAT_INCLUDED, VAT_ADDED])
def test_every_vat_mode_renders(family, vat_mode):
    proposal = with_requisites(demo_proposal(date(2026, 7, 28)))
    proposal.vat_mode = vat_mode
    data, warnings = invoice.render(proposal, family=family)
    assert data.startswith(b"%PDF")
    assert warnings == []


def test_empty_proposal_still_renders(family):
    data, warnings = invoice.render(Proposal(), family=family)
    assert data.startswith(b"%PDF")
    assert warnings, "у пустого счёта обязаны быть предупреждения о реквизитах"


def test_many_items_paginate(family):
    proposal = with_requisites(demo_proposal(date(2026, 7, 28)))
    proposal.items = [
        LineItem(title=f"Работа {n} " + "с длинным названием" * 3, quantity=n, price=1000)
        for n in range(1, 60)
    ]
    data, _ = invoice.render(proposal, family=family)
    count = int(re.search(rb"/Type /Pages /Kids \[(.*?)\] /Count (\d+)", data).group(2))
    assert count >= 2


def test_invoice_has_no_logos(family):
    """В счёте логотипы не печатаются: это бухгалтерский документ."""
    proposal = with_requisites(demo_proposal(date(2026, 7, 28)))
    data, _ = invoice.render(proposal, family=family)
    assert b"/Subtype /Image" not in data


def test_same_input_gives_same_bytes(family):
    first, _ = invoice.render(with_requisites(demo_proposal(date(2026, 7, 28))), family=family)
    second, _ = invoice.render(with_requisites(demo_proposal(date(2026, 7, 28))), family=family)
    assert first == second


def test_has_requisites():
    party = Party(name="ИП")
    assert not invoice.has_requisites(party)

    party.inn = "771234567890"
    assert not invoice.has_requisites(party)

    party.bank = Bank(name="Банк", bik="044525974", account="40802810100000001234")
    assert invoice.has_requisites(party)


# --- реквизиты в модели --------------------------------------------------------


def test_bank_digits_are_cleaned():
    """Номера копируют из платёжек вместе с пробелами."""
    bank = Bank.from_dict({"bik": "044 525 974", "account": "40802 810 1 0000 0001234"})
    assert bank.bik == "044525974"
    assert bank.account == "40802810100000001234"


def test_bank_filled_needs_name_bik_and_account():
    assert not Bank(name="Банк", bik="044525974").filled
    assert not Bank(bik="044525974", account="40802810100000001234").filled
    assert Bank(name="Банк", bik="044525974", account="40802810100000001234").filled


def test_tax_line():
    assert Party(inn="7712345678", kpp="771201001").tax_line == "ИНН 7712345678, КПП 771201001"
    assert Party(inn="7712345678").tax_line == "ИНН 7712345678"
    assert Party().tax_line == ""


def test_full_line_skips_empty_parts():
    party = Party(name="ООО «Ромашка»", inn="7712345678", address="Москва")
    assert party.full_line() == "ИНН 7712345678, ООО «Ромашка», Москва"
    assert Party(name="Только имя").full_line() == "Только имя"
    assert Party().full_line() == ""


def test_party_roundtrip_keeps_requisites():
    party = Party(name="ООО", inn="7712345678", kpp="771201001",
                  bank=Bank(name="Банк", bik="044525974", account="40802810100000001234"))
    restored = Party.from_dict(party.to_dict())
    assert restored.to_dict() == party.to_dict()
    assert restored.bank.filled
