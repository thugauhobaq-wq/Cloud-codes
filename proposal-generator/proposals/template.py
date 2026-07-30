"""Вёрстка коммерческого предложения.

Один проход сверху вниз: каждый блок сообщает, сколько ему нужно места, и если
не влезает — начинается новая страница. Таблица позиций умеет разрываться и
повторяет шапку, колонтитул с номером страницы дорисовывается в самом конце,
когда известно общее количество страниц.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .models import VAT_ADDED, VAT_INCLUDED, LineItem, Party, Proposal
from .money import format_amount, format_percent, format_quantity
from .pdf import (
    A4,
    BOLD,
    ITALIC,
    REGULAR,
    Color,
    Document,
    Image,
    Page,
    find_family,
    mix,
    readable_on,
    rgb,
)
from .pdf.fonts import FontFamily
from .pdf.images import ImageError

MARGIN = 46.0
FOOTER_SPACE = 58.0
CONTENT_WIDTH = A4[0] - MARGIN * 2

INK = rgb("#16181d")
MUTED = rgb("#6b7280")
FAINT = rgb("#98a2b3")
LINE = rgb("#e3e6ec")
SOFT = rgb("#f7f8fa")
WHITE = (1.0, 1.0, 1.0)

TEMPLATES = {
    "classic": "Классический — светлая шапка, акцент тонкой линией",
    "modern": "Современный — плашка акцентного цвета во всю ширину",
}

_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def format_date(value: date) -> str:
    return f"{value.day} {_MONTHS[value.month - 1]} {value.year}"


@dataclass(slots=True)
class Palette:
    accent: Color
    accent_soft: Color
    accent_ink: Color
    on_accent: Color

    @classmethod
    def build(cls, accent_hex: str) -> Palette:
        try:
            accent = rgb(accent_hex)
        except ValueError:
            accent = rgb("#1f5eff")
        return cls(
            accent=accent,
            accent_soft=mix(accent, WHITE, 0.90),
            accent_ink=mix(accent, INK, 0.35),
            on_accent=readable_on(accent),
        )


class Flow:
    """Курсор вёрстки: где мы сейчас и сколько осталось до подвала."""

    def __init__(self, document: Document, chrome) -> None:
        self.document = document
        self._chrome = chrome
        self.page: Page = document.new_page()
        self.y = chrome(self.page, first=True)

    @property
    def bottom(self) -> float:
        return A4[1] - FOOTER_SPACE

    @property
    def left(self) -> float:
        return MARGIN

    @property
    def width(self) -> float:
        return CONTENT_WIDTH

    def need(self, height: float) -> None:
        """Гарантировать `height` пунктов на текущей странице."""
        if self.y + height > self.bottom:
            self.break_page()

    def break_page(self) -> None:
        self.page = self.document.new_page()
        self.y = self._chrome(self.page, first=False)


def render(
    proposal: Proposal,
    *,
    font: str | None = None,
    family: FontFamily | None = None,
) -> tuple[bytes, list[str]]:
    """Собрать PDF предложения.

    Возвращает файл и список предупреждений — логотип не в том формате или
    символы, которых нет в шрифте, не повод отказываться печатать документ.
    """
    warnings: list[str] = []
    family = family or find_family(font)
    palette = Palette.build(proposal.accent)

    document = Document(
        family,
        title=_document_title(proposal),
        author=proposal.sender.name,
        subject=proposal.subject,
    )

    sender_logo = _load_logo(document, proposal.sender.logo, "исполнителя", warnings)
    client_logo = _load_logo(document, proposal.client.logo, "заказчика", warnings)

    style = proposal.template if proposal.template in TEMPLATES else "classic"

    def chrome(page: Page, first: bool) -> float:
        """Шапка страницы; возвращает `y`, с которого можно писать."""
        if first:
            return _draw_cover(page, proposal, palette, sender_logo, client_logo, style)
        return _draw_running_header(page, proposal, palette)

    flow = Flow(document, chrome)

    _draw_title(flow, proposal, palette)
    _draw_parties(flow, proposal, palette, client_logo)
    _draw_intro(flow, proposal)
    _draw_sections(flow, proposal, palette)
    _draw_items(flow, proposal, palette)
    _draw_totals(flow, proposal, palette)
    _draw_terms(flow, proposal, palette)
    _draw_notes(flow, proposal)
    if proposal.show_signature:
        _draw_signature(flow, proposal, palette)

    _draw_footers(document, proposal, palette)

    if document.missing_glyphs:
        sample = "".join(sorted(document.missing_glyphs)[:12])
        warnings.append(
            f"шрифт {family.name} не содержит символов: {sample} — они не напечатаются"
        )
    return document.render(), warnings


def _document_title(proposal: Proposal) -> str:
    title = f"Коммерческое предложение № {proposal.number}"
    if proposal.subject:
        title = f"{title} — {proposal.subject}"
    return title


def _load_logo(
    document: Document, data: bytes | None, whose: str, warnings: list[str]
) -> Image | None:
    if not data:
        return None
    try:
        return document.add_image(data)
    except ImageError as error:
        warnings.append(f"логотип {whose} не вставлен: {error}")
        return None


# --- шапка первой страницы -----------------------------------------------------


def _draw_cover(
    page: Page,
    proposal: Proposal,
    palette: Palette,
    sender_logo: Image | None,
    client_logo: Image | None,
    style: str,
) -> float:
    if style == "modern":
        return _cover_modern(page, proposal, palette, sender_logo)
    return _cover_classic(page, proposal, palette, sender_logo)


def _cover_classic(
    page: Page, proposal: Proposal, palette: Palette, sender_logo: Image | None
) -> float:
    page.rect(0, 0, A4[0], 5, fill=palette.accent)

    top = 34.0
    box_height = 40.0
    if sender_logo is not None:
        page.image_fit(sender_logo, MARGIN, top, 150, box_height)
    else:
        page.text(
            MARGIN,
            top + 6,
            proposal.sender.name or "Исполнитель",
            size=16,
            style=BOLD,
            color=INK,
        )

    _sender_contacts(page, proposal.sender, top, box_height)
    page.line(MARGIN, top + box_height + 18, A4[0] - MARGIN, top + box_height + 18, color=LINE)
    return top + box_height + 40


def _cover_modern(
    page: Page, proposal: Proposal, palette: Palette, sender_logo: Image | None
) -> float:
    band = 96.0
    page.rect(0, 0, A4[0], band, fill=palette.accent)

    top = 30.0
    if sender_logo is not None:
        page.image_fit(sender_logo, MARGIN, top, 150, 40)
    else:
        page.text(
            MARGIN,
            top + 8,
            proposal.sender.name or "Исполнитель",
            size=17,
            style=BOLD,
            color=palette.on_accent,
        )

    contacts = proposal.sender.contacts
    y = top + 2
    for line in contacts[:3]:
        y = page.text(
            A4[0] - MARGIN - 200,
            y,
            line,
            size=8.5,
            color=palette.on_accent,
            align="right",
            width=200,
        )
    return band + 30


def _sender_contacts(page: Page, sender: Party, top: float, box_height: float) -> None:
    lines = sender.contacts
    if not lines:
        return
    y = top + max(0.0, (box_height - len(lines) * 11.5) / 2)
    for line in lines[:3]:
        y = page.text(
            A4[0] - MARGIN - 220, y, line, size=8.5, color=MUTED, align="right", width=220
        )


def _draw_running_header(page: Page, proposal: Proposal, palette: Palette) -> float:
    """Шапка второй и следующих страниц — узкая, чтобы не съедать место."""
    page.rect(0, 0, A4[0], 3, fill=palette.accent)
    top = 28.0
    page.text(
        MARGIN,
        top,
        proposal.sender.name or "Коммерческое предложение",
        size=9,
        style=BOLD,
        color=INK,
    )
    page.text(
        MARGIN,
        top,
        f"Коммерческое предложение № {proposal.number} от {format_date(proposal.issued_at)}",
        size=8.5,
        color=FAINT,
        align="right",
        width=CONTENT_WIDTH,
    )
    page.line(MARGIN, top + 16, A4[0] - MARGIN, top + 16, color=LINE)
    return top + 34


# --- заголовок и стороны -------------------------------------------------------


def _draw_title(flow: Flow, proposal: Proposal, palette: Palette) -> None:
    page = flow.page
    y = page.text(
        flow.left, flow.y, "Коммерческое предложение", size=23, style=BOLD, color=INK
    )
    y += 3

    meta = f"№ {proposal.number} от {format_date(proposal.issued_at)}"
    if proposal.valid_days > 0:
        meta += f"    ·    действительно до {format_date(proposal.valid_until)}"
    y = page.text(flow.left, y, meta, size=9, color=MUTED)

    if proposal.subject:
        y += 10
        y = page.paragraph(
            flow.left,
            y,
            flow.width,
            proposal.subject,
            size=13.5,
            style=BOLD,
            color=palette.accent_ink,
            leading=1.3,
        )
    flow.y = y + 18


def _draw_parties(
    flow: Flow, proposal: Proposal, palette: Palette, client_logo: Image | None
) -> None:
    """Две карточки: кто предлагает и кому."""
    gap = 16.0
    card_width = (flow.width - gap) / 2

    left_lines = _party_lines(proposal.sender)
    right_lines = _party_lines(proposal.client)
    height = _card_height(flow.document, left_lines, right_lines, card_width, client_logo)

    flow.need(height + 12)
    page, top = flow.page, flow.y

    _party_card(page, flow.left, top, card_width, height, "Исполнитель", left_lines, palette, None)
    _party_card(
        page,
        flow.left + card_width + gap,
        top,
        card_width,
        height,
        "Заказчик",
        right_lines,
        palette,
        client_logo,
    )
    flow.y = top + height + 22


def _party_lines(party: Party) -> list[tuple[str, str]]:
    """(текст, стиль) — то, что печатается в карточке стороны."""
    lines: list[tuple[str, str]] = []
    if party.name:
        lines.append((party.name, BOLD))
    if party.person:
        who = f"{party.position}: {party.person}" if party.position else party.person
        lines.append((who, REGULAR))
    for contact in party.contacts:
        lines.append((contact, REGULAR))
    if party.address:
        lines.append((party.address, REGULAR))
    if party.details:
        lines.append((party.details, REGULAR))
    return lines


def _card_height(
    document: Document,
    left: list[tuple[str, str]],
    right: list[tuple[str, str]],
    width: float,
    client_logo: Image | None,
) -> float:
    inner = width - 28
    def block(lines: list[tuple[str, str]], logo: Image | None) -> float:
        height = 30.0  # заголовок карточки
        if logo is not None:
            height += 42.0
        for text, style in lines:
            size = 10.5 if style == BOLD else 9
            height += document.text_height(text, inner, size=size, leading=1.3, style=style) + 2
        return height + 16

    return max(block(left, None), block(right, client_logo), 96.0)


def _party_card(
    page: Page,
    x: float,
    y: float,
    width: float,
    height: float,
    caption: str,
    lines: list[tuple[str, str]],
    palette: Palette,
    logo: Image | None,
) -> None:
    page.round_rect(x, y, width, height, 8, fill=SOFT, stroke=LINE, line_width=0.5)
    inner_x = x + 14
    inner = width - 28

    page.text(inner_x, y + 12, caption.upper(), size=7.5, style=BOLD,
              color=palette.accent, char_spacing=0.8)
    cursor = y + 30

    if logo is not None:
        # Логотип клиента — главный смысл всей затеи: даём ему честный блок и
        # прижимаем влево, чтобы он читался как часть карточки, а не наклейка.
        _, drawn_height = page.image_fit(logo, inner_x, cursor, min(inner, 150), 34)
        cursor += max(drawn_height, 34) + 8

    for text, style in lines:
        size = 10.5 if style == BOLD else 9
        color = INK if style == BOLD else MUTED
        cursor = page.paragraph(
            inner_x, cursor, inner, text, size=size, style=style, color=color, leading=1.3
        )
        cursor += 2


def _draw_intro(flow: Flow, proposal: Proposal) -> None:
    if not proposal.intro:
        return
    flow.document.check_glyphs(proposal.intro)
    height = flow.document.text_height(proposal.intro, flow.width, size=10, leading=1.5)
    flow.need(min(height, 80))
    flow.y = flow.page.paragraph(
        flow.left, flow.y, flow.width, proposal.intro, size=10, color=INK, leading=1.5
    )
    flow.y += 20


def _draw_sections(flow: Flow, proposal: Proposal, palette: Palette) -> None:
    for block in proposal.sections:
        flow.document.check_glyphs(block.title + block.body)
        heading = flow.document.text_height(block.title, flow.width, size=12.5, style=BOLD)
        # Не оставляем заголовок в одиночестве внизу страницы.
        flow.need(heading + 40)
        if block.title:
            flow.y = flow.page.text(
                flow.left, flow.y, block.title, size=12.5, style=BOLD, color=INK
            )
            flow.y += 6
        if block.body:
            flow.y = _flowing_paragraph(flow, block.body, size=9.8, color=INK, leading=1.5)
        flow.y += 16


def _flowing_paragraph(
    flow: Flow, text: str, *, size: float, color: Color, leading: float
) -> float:
    """Абзац, который умеет переехать на следующую страницу по строкам."""
    step = size * leading
    lines = flow.document.wrap(text, flow.width, size=size)
    for line in lines:
        if flow.y + step > flow.bottom:
            flow.break_page()
        flow.page.text(flow.left, flow.y, line, size=size, color=color)
        flow.y += step
    return flow.y


# --- таблица позиций -----------------------------------------------------------

_COLUMNS = (
    ("№", 24.0, "left"),
    ("Наименование", 0.0, "left"),  # 0 — «занять всё оставшееся»
    ("Кол-во", 52.0, "right"),
    ("Ед.", 36.0, "center"),
    ("Цена", 78.0, "right"),
    ("Сумма", 84.0, "right"),
)
ROW_PADDING = 9.0


def _column_widths(total: float) -> list[float]:
    fixed = sum(width for _, width, _ in _COLUMNS if width)
    return [width or (total - fixed) for _, width, _ in _COLUMNS]


def _draw_items(flow: Flow, proposal: Proposal, palette: Palette) -> None:
    if not proposal.items:
        return

    widths = _column_widths(flow.width)
    header_height = 26.0

    flow.need(header_height + 46)
    _table_header(flow, palette, widths, header_height)

    for index, item in enumerate(proposal.items, start=1):
        flow.document.check_glyphs(item.title + item.description)
        height = _row_height(flow.document, item, widths)
        if flow.y + height > flow.bottom:
            flow.break_page()
            _table_header(flow, palette, widths, header_height)
        _draw_row(flow, index, item, proposal.currency, widths, height, palette)

    flow.page.line(flow.left, flow.y, flow.left + flow.width, flow.y, color=LINE, width=1.0)
    flow.y += 18


def _table_header(flow: Flow, palette: Palette, widths: list[float], height: float) -> None:
    page = flow.page
    page.rect(flow.left, flow.y, flow.width, height, fill=palette.accent_soft)
    x = flow.left
    for (caption, _, align), width in zip(_COLUMNS, widths, strict=True):
        page.text(
            x + ROW_PADDING,
            flow.y + 8,
            caption,
            size=8,
            style=BOLD,
            color=palette.accent_ink,
            align=align,
            width=width - ROW_PADDING * 2,
        )
        x += width
    flow.y += height


def _row_height(document: Document, item: LineItem, widths: list[float]) -> float:
    inner = widths[1] - ROW_PADDING * 2
    height = document.text_height(item.title or "—", inner, size=9.8, leading=1.3, style=BOLD)
    if item.description:
        height += 3 + document.text_height(item.description, inner, size=8.6, leading=1.35)
    return max(height + 18, 34.0)


def _draw_row(
    flow: Flow,
    index: int,
    item: LineItem,
    currency: str,
    widths: list[float],
    height: float,
    palette: Palette,
) -> None:
    page, top = flow.page, flow.y
    if index % 2 == 0:
        page.rect(flow.left, top, flow.width, height, fill=rgb("#fbfcfd"))
    page.line(flow.left, top, flow.left + flow.width, top, color=LINE, width=0.5)

    x = flow.left
    text_top = top + 9

    page.text(x + ROW_PADDING, text_top, str(index), size=9, color=FAINT)
    x += widths[0]

    inner = widths[1] - ROW_PADDING * 2
    cursor = page.paragraph(
        x + ROW_PADDING, text_top, inner, item.title or "—",
        size=9.8, style=BOLD, color=INK, leading=1.3,
    )
    if item.description:
        page.paragraph(
            x + ROW_PADDING, cursor + 3, inner, item.description,
            size=8.6, color=MUTED, leading=1.35,
        )
    x += widths[1]

    def cell(width: float, text: str, *, align: str, size: float = 9.5,
             color: Color = INK, style: str = REGULAR) -> None:
        page.text(
            x + ROW_PADDING, text_top, text, size=size, style=style,
            color=color, align=align, width=width - ROW_PADDING * 2,
        )

    cell(widths[2], format_quantity(item.quantity), align="right")
    x += widths[2]
    cell(widths[3], item.unit, align="center", size=8.8, color=MUTED)
    x += widths[3]
    cell(widths[4], format_amount(item.price, currency, with_symbol=False), align="right")
    if item.discount_percent:
        page.text(
            x + ROW_PADDING,
            text_top + 13,
            f"−{format_percent(item.discount_percent)}%",
            size=8,
            color=palette.accent,
            align="right",
            width=widths[4] - ROW_PADDING * 2,
        )
    x += widths[4]
    cell(widths[5], format_amount(item.total, currency, with_symbol=False),
         align="right", style=BOLD)

    flow.y = top + height


# --- итоги, условия, подпись ---------------------------------------------------


def _draw_totals(flow: Flow, proposal: Proposal, palette: Palette) -> None:
    if not proposal.items:
        return
    totals = proposal.totals()

    rows: list[tuple[str, str, bool]] = []
    if totals.item_discount or totals.discount:
        rows.append(("Сумма по позициям", format_amount(
            totals.subtotal + totals.item_discount, proposal.currency), False))
    if totals.item_discount:
        rows.append(("Скидки по позициям", "−" + format_amount(
            totals.item_discount, proposal.currency), False))
    if totals.discount:
        rows.append((
            f"Скидка {format_percent(proposal.discount_percent)}%",
            "−" + format_amount(totals.discount, proposal.currency),
            False,
        ))
    if proposal.vat_mode == VAT_ADDED:
        rows.append(("Сумма без НДС", format_amount(totals.net, proposal.currency), False))
        rows.append((proposal.vat_note, format_amount(totals.vat, proposal.currency), False))
    elif proposal.vat_mode == VAT_INCLUDED:
        rows.append((proposal.vat_note, format_amount(totals.vat, proposal.currency), False))

    panel_width = 300.0
    panel_x = flow.left + flow.width - panel_width
    row_height = 19.0
    total_height = 46.0
    words_height = flow.document.text_height(
        f"Сумма прописью: {totals.in_words}", flow.width, size=8.8, leading=1.35, style=ITALIC
    )
    block = len(rows) * row_height + total_height + words_height + 18
    flow.need(block)

    page, top = flow.page, flow.y
    y = top
    for caption, value, _ in rows:
        page.text(panel_x, y + 4, caption, size=9, color=MUTED)
        page.text(panel_x, y + 4, value, size=9, color=INK, align="right", width=panel_width)
        y += row_height

    page.round_rect(panel_x, y, panel_width, total_height, 8, fill=palette.accent)
    page.text(panel_x + 16, y + 9, "Итого к оплате", size=9, color=palette.on_accent)
    page.text(
        panel_x + 16,
        y + 22,
        format_amount(totals.total, proposal.currency),
        size=16,
        style=BOLD,
        color=palette.on_accent,
        align="right",
        width=panel_width - 32,
    )
    y += total_height + 10

    if proposal.vat_mode != VAT_ADDED and proposal.vat_mode != VAT_INCLUDED:
        page.text(panel_x, y - 4, proposal.vat_note, size=8.2, color=FAINT,
                  align="right", width=panel_width)

    words = f"Сумма прописью: {totals.in_words}"
    flow.document.check_glyphs(words, ITALIC)
    y = page.paragraph(flow.left, y + 4, flow.width, words,
                       size=8.8, style=ITALIC, color=MUTED, leading=1.35)
    flow.y = max(y, top + block - 18) + 20


def _draw_terms(flow: Flow, proposal: Proposal, palette: Palette) -> None:
    terms: list[tuple[str, str]] = []
    if proposal.lead_time:
        terms.append(("Срок выполнения", proposal.lead_time))
    if proposal.payment_terms:
        terms.append(("Условия оплаты", proposal.payment_terms))
    if proposal.valid_days > 0:
        terms.append(("Действительно до", format_date(proposal.valid_until)))
    if not terms:
        return

    gap = 12.0
    card_width = (flow.width - gap * (len(terms) - 1)) / len(terms)
    inner = card_width - 28
    height = 20.0 + max(
        flow.document.text_height(value, inner, size=9.5, leading=1.35) for _, value in terms
    ) + 34

    flow.need(height + 10)
    page, top = flow.page, flow.y
    x = flow.left
    for caption, value in terms:
        flow.document.check_glyphs(value)
        page.round_rect(x, top, card_width, height, 8, stroke=LINE, line_width=0.8)
        # Разрядку 0.6 pt на символ ellipsize не учитывает — закладываем её в запас.
        label = flow.document.ellipsize(
            caption.upper(), inner - len(caption) * 0.6, size=7.2, style=BOLD
        )
        page.text(x + 14, top + 13, label, size=7.2, style=BOLD,
                  color=palette.accent, char_spacing=0.6)
        page.paragraph(x + 14, top + 30, inner, value, size=9.5, color=INK, leading=1.35)
        x += card_width + gap
    flow.y = top + height + 20


def _draw_notes(flow: Flow, proposal: Proposal) -> None:
    if not proposal.notes:
        return
    flow.document.check_glyphs(proposal.notes)
    flow.need(30)
    flow.y = _flowing_paragraph(flow, proposal.notes, size=8.8, color=MUTED, leading=1.45)
    flow.y += 18


def _draw_signature(flow: Flow, proposal: Proposal, palette: Palette) -> None:
    height = 64.0
    flow.need(height)
    page, top = flow.page, flow.y

    column = min(230.0, flow.width / 2)
    # Выравнивание вправо у длинной строки съезжает влево, на чужую половину
    # блока, — обрезаем всё, что шире своей колонки.
    def fit(text: str, size: float, style: str = REGULAR) -> str:
        return flow.document.ellipsize(text, column, size=size, style=style)

    page.line(flow.left, top + 30, flow.left + column, top + 30, color=rgb("#c8cdd6"))
    page.text(flow.left, top + 36, "подпись", size=7.5, color=FAINT)

    who = proposal.sender.person or proposal.sender.name
    if who:
        page.text(flow.left, top + 8, fit(who, 9.5, BOLD), size=9.5, style=BOLD, color=INK,
                  align="right", width=column)
    if proposal.sender.position:
        page.text(flow.left, top + 36, fit(proposal.sender.position, 7.5), size=7.5,
                  color=FAINT, align="right", width=column)

    right_x = flow.left + flow.width - column
    page.text(right_x, top + 8, "С уважением,", size=9, color=MUTED,
              align="right", width=column)
    page.text(
        right_x,
        top + 22,
        fit(proposal.sender.name, 9.5, BOLD),
        size=9.5,
        style=BOLD,
        color=palette.accent_ink,
        align="right",
        width=column,
    )
    flow.y = top + height


def _draw_footers(document: Document, proposal: Proposal, palette: Palette) -> None:
    """Колонтитул на каждой странице — рисуем, когда все страницы уже созданы."""
    total = document.page_count
    contacts = "   ·   ".join(proposal.sender.contacts)

    for number, page in enumerate(document.pages, start=1):
        y = A4[1] - 34
        page.line(MARGIN, y, A4[0] - MARGIN, y, color=LINE, width=0.5)
        if contacts:
            page.text(MARGIN, y + 8, contacts, size=7.6, color=FAINT)
        label = f"стр. {number} из {total}" if total > 1 else ""
        if label:
            page.text(MARGIN, y + 8, label, size=7.6, color=FAINT,
                      align="right", width=CONTENT_WIDTH)
