"""Счёт на оплату из того же черновика.

Предложение приняли — дальше нужен счёт, и данные для него уже есть: стороны,
позиции, суммы. Меняется только форма документа.

Вёрстка нарочно консервативная: шапка с банковскими реквизитами в рамке,
«Счёт на оплату № N от даты», поставщик, покупатель, таблица, итого, сумма
прописью, подписи. Так счёт выглядит в 1С и в любой бухгалтерии — бухгалтер
находит нужное поле не читая, и лишний повод для вопросов не возникает.
"""

from __future__ import annotations

from datetime import date

from .models import VAT_ADDED, VAT_INCLUDED, Party, Proposal
from .money import amount_in_words, format_amount, format_quantity, plural
from .pdf import A4, BOLD, Document, Page, find_family, rgb
from .pdf.fonts import FontFamily
from .template import FOOTER_SPACE, INK, LINE, MARGIN, MUTED, Flow, format_date

CONTENT_WIDTH = A4[0] - MARGIN * 2

BOX_LINE = rgb("#000000")
"""Рамку шапки печатают чёрным: счёт часто идёт в чёрно-белую печать и в скан."""


def render(
    proposal: Proposal,
    *,
    number: str = "",
    issued_at: date | None = None,
    font: str | None = None,
    family: FontFamily | None = None,
) -> tuple[bytes, list[str]]:
    """Собрать счёт. Возвращает файл и список предупреждений.

    Номер и дата счёта по умолчанию берутся у предложения: у небольшой студии
    счёт и КП идут парой и нумеруются одинаково.
    """
    warnings: list[str] = []
    family = family or find_family(font)
    number = number.strip() or proposal.number
    issued_at = issued_at or proposal.issued_at

    sender = proposal.sender
    if not sender.bank.filled:
        warnings.append(
            "не заполнены банковские реквизиты исполнителя — по такому счёту не заплатят"
        )
    if not sender.inn:
        warnings.append("не указан ИНН исполнителя")

    document = Document(
        family,
        title=f"Счёт на оплату № {number} от {format_date(issued_at)}",
        author=sender.name,
        subject=proposal.subject,
    )

    def chrome(page: Page, first: bool) -> float:
        if first:
            return _header(page, proposal, number, issued_at)
        return _running_header(page, number, issued_at)

    flow = Flow(document, chrome)

    _parties(flow, proposal)
    _items(flow, proposal)
    _totals(flow, proposal)
    _signatures(flow, proposal)
    _stamp_page_numbers(document)

    if document.missing_glyphs:
        sample = "".join(sorted(document.missing_glyphs)[:12])
        warnings.append(f"шрифт {family.name} не содержит символов: {sample}")
    return document.render(), warnings


# --- шапка ---------------------------------------------------------------------

# Ширины колонок шапки. Правая пара считается от числа: расчётный счёт — это
# 20 цифр, и они обязаны поместиться целиком, иначе счёт бесполезен.
_LABEL = 118.0
_TAG = 46.0
_NUMBER = 132.0
_VALUE = CONTENT_WIDTH - _LABEL - _TAG - _NUMBER

_ROW = 22.0
_NUMBER_SIZE = 8.5
"""Двадцать цифр при 9 pt уже не влезают в свою клетку."""


def _grid(page: Page, top: float, rows: int) -> tuple[float, float, float]:
    """Нарисовать рамку на `rows` строк и вернуть границы колонок."""
    height = _ROW * rows
    page.rect(MARGIN, top, CONTENT_WIDTH, height, stroke=BOX_LINE, line_width=0.8)
    value_x = MARGIN + _LABEL
    tag_x = value_x + _VALUE
    number_x = tag_x + _TAG
    for x in (value_x, tag_x, number_x):
        page.line(x, top, x, top + height, color=BOX_LINE, width=0.8)
    return value_x, tag_x, number_x


def _header(page: Page, proposal: Proposal, number: str, issued_at: date) -> float:
    document = page.document
    bank = proposal.sender.bank
    sender = proposal.sender
    top = 40.0

    def label(x: float, y: float, text: str) -> None:
        page.text(x + 6, y + 6, text, size=8, color=MUTED)

    def figure(x: float, y: float, text: str) -> None:
        page.text(
            x + 6,
            y + 6,
            document.ellipsize(text, _NUMBER - 12, size=_NUMBER_SIZE),
            size=_NUMBER_SIZE,
            color=INK,
        )

    # Первая рамка: банк получателя, БИК и корреспондентский счёт.
    value_x, tag_x, number_x = _grid(page, top, 2)
    label(MARGIN, top, "Банк получателя")
    page.paragraph(
        value_x + 6, top + 5, _VALUE - 12, bank.name, size=9, color=INK, leading=1.15
    )
    label(tag_x, top, "БИК")
    figure(number_x, top, bank.bik)
    label(tag_x, top + _ROW, "Сч. №")
    figure(number_x, top + _ROW, bank.corr_account)

    # Вторая рамка: реквизиты получателя и его расчётный счёт.
    top += _ROW * 2
    value_x, tag_x, number_x = _grid(page, top, 2)
    label(MARGIN, top, "ИНН / КПП")
    numbers = " / ".join(part for part in (sender.inn, sender.kpp) if part)
    page.text(value_x + 6, top + 6, numbers, size=9, color=INK)
    label(tag_x, top, "Сч. №")
    figure(number_x, top, bank.account)
    label(MARGIN, top + _ROW, "Получатель")
    page.paragraph(
        value_x + 6,
        top + _ROW + 5,
        _VALUE - 12,
        sender.name,
        size=9,
        style=BOLD,
        color=INK,
        leading=1.15,
    )

    top += _ROW * 2 + 26
    title = f"Счёт на оплату № {number} от {format_date(issued_at)}"
    document.check_glyphs(title, BOLD)
    y = page.text(MARGIN, top, title, size=17, style=BOLD, color=INK)
    page.line(MARGIN, y + 8, A4[0] - MARGIN, y + 8, color=BOX_LINE, width=1.6)
    return y + 26


def _running_header(page: Page, number: str, issued_at: date) -> float:
    top = 30.0
    page.text(
        MARGIN,
        top,
        f"Счёт на оплату № {number} от {format_date(issued_at)}",
        size=9,
        style=BOLD,
        color=INK,
    )
    page.line(MARGIN, top + 15, A4[0] - MARGIN, top + 15, color=LINE)
    return top + 32


# --- стороны -------------------------------------------------------------------


def _parties(flow: Flow, proposal: Proposal) -> None:
    for caption, party in (("Поставщик", proposal.sender), ("Покупатель", proposal.client)):
        text = party.full_line() or "—"
        flow.document.check_glyphs(text)
        height = flow.document.text_height(text, CONTENT_WIDTH - 92, size=9, leading=1.3)
        flow.need(height + 10)

        flow.page.text(flow.left, flow.y, f"{caption}:", size=8.5, color=MUTED)
        end = flow.page.paragraph(
            flow.left + 92, flow.y - 1, CONTENT_WIDTH - 92, text, size=9, color=INK, leading=1.3
        )
        flow.y = end + 8
    flow.y += 8


# --- таблица -------------------------------------------------------------------

_COLUMNS = (
    ("№", 26.0, "center"),
    ("Товары (работы, услуги)", 0.0, "left"),
    ("Кол-во", 50.0, "right"),
    ("Ед.", 56.0, "center"),
    ("Цена", 80.0, "right"),
    ("Сумма", 86.0, "right"),
)
PADDING = 6.0


def _widths() -> list[float]:
    fixed = sum(width for _, width, _ in _COLUMNS if width)
    return [width or (CONTENT_WIDTH - fixed) for _, width, _ in _COLUMNS]


def _items(flow: Flow, proposal: Proposal) -> None:
    widths = _widths()
    flow.need(60)
    _table_header(flow, widths)

    for index, item in enumerate(proposal.items, start=1):
        flow.document.check_glyphs(item.title)
        height = _row_height(flow, item, widths)
        if flow.y + height > flow.bottom:
            flow.break_page()
            _table_header(flow, widths)
        _row(flow, index, item, proposal.currency, widths, height)

    flow.page.line(flow.left, flow.y, flow.left + CONTENT_WIDTH, flow.y,
                   color=BOX_LINE, width=0.8)


def _table_header(flow: Flow, widths: list[float]) -> None:
    height = 24.0
    page = flow.page
    page.rect(flow.left, flow.y, CONTENT_WIDTH, height, fill=rgb("#f2f3f5"))
    page.line(flow.left, flow.y, flow.left + CONTENT_WIDTH, flow.y, color=BOX_LINE, width=0.8)

    x = flow.left
    for (caption, _, align), width in zip(_COLUMNS, widths, strict=True):
        page.text(
            x + PADDING, flow.y + 7, caption, size=8, style=BOLD, color=INK,
            align=align, width=width - PADDING * 2,
        )
        x += width
        if x < flow.left + CONTENT_WIDTH - 1:
            page.line(x, flow.y, x, flow.y + height, color=BOX_LINE, width=0.5)
    flow.y += height


def _row_height(flow: Flow, item, widths: list[float]) -> float:
    inner = widths[1] - PADDING * 2
    height = flow.document.text_height(item.title or "—", inner, size=9, leading=1.25)
    return max(height + 12, 24.0)


def _row(flow: Flow, index: int, item, currency: str, widths: list[float],
         height: float) -> None:
    page, top = flow.page, flow.y
    page.line(flow.left, top, flow.left + CONTENT_WIDTH, top, color=LINE, width=0.5)

    x = flow.left
    text_top = top + 6
    cells = (
        (str(index), "center", 9),
        (item.title or "—", "left", 9),
        (format_quantity(item.quantity), "right", 9),
        (flow.document.ellipsize(item.unit, widths[3] - PADDING * 2, size=8.5), "center", 8.5),
        (format_amount(item.price, currency, with_symbol=False), "right", 9),
        (format_amount(item.total, currency, with_symbol=False), "right", 9),
    )
    for position, ((text, align, size), width) in enumerate(zip(cells, widths, strict=True)):
        if position == 1:
            page.paragraph(
                x + PADDING, text_top, width - PADDING * 2, text,
                size=size, color=INK, leading=1.25,
            )
        else:
            page.text(
                x + PADDING, text_top, text, size=size, color=INK,
                align=align, width=width - PADDING * 2,
            )
        x += width
        if x < flow.left + CONTENT_WIDTH - 1:
            page.line(x, top, x, top + height, color=BOX_LINE, width=0.5)
    flow.y = top + height


# --- итоги ---------------------------------------------------------------------


def _totals(flow: Flow, proposal: Proposal) -> None:
    totals = proposal.totals()
    rows: list[tuple[str, str, bool]] = []

    if totals.item_discount or totals.discount:
        rows.append(("Сумма по позициям",
                     format_amount(totals.subtotal + totals.item_discount, proposal.currency),
                     False))
        skidka = totals.item_discount + totals.discount
        rows.append(("Скидка", "−" + format_amount(skidka, proposal.currency), False))

    if proposal.vat_mode == VAT_ADDED:
        rows.append(("Итого", format_amount(totals.net, proposal.currency), False))
        rows.append((proposal.vat_note, format_amount(totals.vat, proposal.currency), False))
    elif proposal.vat_mode == VAT_INCLUDED:
        rows.append(("Итого", format_amount(totals.total, proposal.currency), False))
        rows.append((proposal.vat_note, format_amount(totals.vat, proposal.currency), False))
    else:
        rows.append(("Итого", format_amount(totals.total, proposal.currency), False))
        rows.append((proposal.vat_note, "", False))

    rows.append(("Всего к оплате", format_amount(totals.total, proposal.currency), True))

    flow.need(len(rows) * 18 + 60)
    page = flow.page
    flow.y += 10

    label_width = 200.0
    value_width = 130.0
    label_x = flow.left + CONTENT_WIDTH - label_width - value_width
    for caption, value, strong in rows:
        size = 11 if strong else 9
        style = BOLD if strong else "regular"
        page.text(label_x, flow.y, caption, size=size, style=style, color=INK,
                  align="right", width=label_width)
        page.text(label_x + label_width, flow.y, value, size=size, style=style, color=INK,
                  align="right", width=value_width)
        flow.y += size * 1.7

    flow.y += 8
    count = len(proposal.items)
    words = amount_in_words(totals.total, proposal.currency)
    word = plural(count, "наименование", "наименования", "наименований")
    summary = (
        f"Всего {word} {count}, на сумму "
        f"{format_amount(totals.total, proposal.currency)}."
    )
    flow.document.check_glyphs(summary + words)
    flow.y = page.paragraph(flow.left, flow.y, CONTENT_WIDTH, summary, size=9,
                            color=INK, leading=1.35)
    flow.y = page.paragraph(flow.left, flow.y + 2, CONTENT_WIDTH, f"{words}.",
                            size=9.5, style=BOLD, color=INK, leading=1.35)
    flow.y += 6
    page.line(flow.left, flow.y, flow.left + CONTENT_WIDTH, flow.y, color=BOX_LINE, width=1.2)
    flow.y += 20


def _signatures(flow: Flow, proposal: Proposal) -> None:
    """Две подписи и место под печать — то, что ждут от счёта."""
    flow.need(90)
    page, top = flow.page, flow.y
    column = 250.0

    who = proposal.sender.person or proposal.sender.name
    for index, caption in enumerate(("Руководитель", "Бухгалтер")):
        line_y = top + index * 34 + 16
        page.text(flow.left, line_y - 8, caption, size=9, color=INK)
        page.line(flow.left + 90, line_y, flow.left + 90 + 150, line_y, color=rgb("#909090"))
        page.text(flow.left + 250, line_y - 8,
                  flow.document.ellipsize(who, column - 20, size=9), size=9, color=INK)

    page.text(A4[0] - MARGIN - 120, top + 58, "М.П.", size=9, color=MUTED,
              align="center", width=120)
    flow.y = top + 84

    note = (
        "Оплата счёта означает согласие с условиями предложения. "
        "Товары (работы, услуги) отпускаются по факту оплаты."
    )
    if proposal.payment_terms:
        note = f"Условия оплаты: {proposal.payment_terms}. {note}"
    flow.document.check_glyphs(note)
    flow.y = page.paragraph(flow.left, flow.y, CONTENT_WIDTH, note, size=8,
                            color=MUTED, leading=1.4)


def _stamp_page_numbers(document: Document) -> None:
    total = document.page_count
    if total < 2:
        return
    for number, page in enumerate(document.pages, start=1):
        page.text(
            MARGIN,
            A4[1] - FOOTER_SPACE + 24,
            f"стр. {number} из {total}",
            size=8,
            color=MUTED,
            align="right",
            width=CONTENT_WIDTH,
        )


def has_requisites(party: Party) -> bool:
    """Можно ли вообще выставлять счёт от этой стороны."""
    return party.bank.filled and bool(party.inn)
