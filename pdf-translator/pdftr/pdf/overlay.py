"""Сборка переведённого PDF поверх исходного.

Заманчиво собрать документ заново — но тогда пропадут картинки, рамки, печати,
подписи и вся вёрстка, ради которой человек и прислал именно PDF. Поэтому
исходный файл не пересобирается, а дополняется:

1. Все объекты исходника переносятся в новый файл как есть.
2. В контент-потоке страницы операторы, нарисовавшие переведённый текст,
   переключаются в режим «рисовать невидимо»: исходные буквы исчезают, но
   ничего не сдвигается — ни ширины, ни следующие строки, ни картинки.
3. Сверху дописывается наш поток: перевод в тех же рамках, тем же кеглем и с
   тем же выключением, что было у оригинала.

Заклейка фоном осталась запасным вариантом — для текста внутри Form XObject.
Такой объект может использоваться сразу несколькими страницами, и правка его
потока испортила бы соседние.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .embed import FontFace, FontFamily, load
from .layout import Block
from .objects import Name, PDFFile, Ref, Stream, TextString, fmt_number
from .reader import Document, Page, StreamObject

# Насколько сильно разрешено ужимать кегль, чтобы перевод влез в исходную рамку.
MIN_SCALE = 0.62
MIN_SIZE = 4.0


def _n(value: float) -> str:
    return fmt_number(round(value, 3))


def _colour(rgb: tuple[float, float, float]) -> str:
    return " ".join(_n(channel) for channel in rgb)


@dataclass(frozen=True, slots=True)
class Fixed:
    """Значение, уже переведённое в нумерацию нового файла.

    Страницы собираются раньше, чем копируется дерево документа, и ссылки в них
    указывают на новые объекты. Без этой пометки копировщик принял бы их за
    ссылки исходника и подменил ещё раз.
    """

    value: object


class Copier:
    """Перенос объектов исходного документа в собираемый файл.

    Копируем только то, до чего можно дойти от каталога: мусор, оставшийся в
    файле от прошлых правок, тащить в результат незачем. Страницы подменяются
    через `overrides` — там лежат уже исправленные словари.
    """

    def __init__(self, doc: Document, pdf: PDFFile) -> None:
        self.doc = doc
        self.pdf = pdf
        self.map: dict[int, Ref] = {}
        self.overrides: dict[int, object] = {}

    def ref_for(self, number: int) -> Ref:
        """Ссылка на копию объекта. Номер выдаётся сразу, содержимое — следом."""
        known = self.map.get(number)
        if known is not None:
            return known
        placeholder = self.pdf.reserve()
        self.map[number] = placeholder
        source = self.overrides.get(number)
        if source is None:
            source = self.doc.get(number)
        self.pdf.assign(placeholder, self.convert(source))
        return placeholder

    def convert(self, value: object, depth: int = 0) -> object:
        if depth > 96:
            return None
        if isinstance(value, Fixed):
            return value.value
        if isinstance(value, Ref):
            return self.ref_for(value.num)
        if isinstance(value, StreamObject):
            info = {
                key: self.convert(item, depth + 1)
                for key, item in value.info.items()
                if key != "Length"
            }
            # Данные переносим сжатыми, как лежали: разжимать и жать заново —
            # это минуты на большом документе и ноль пользы.
            return Stream(value.raw, info, compress=False)
        if isinstance(value, dict):
            return {key: self.convert(item, depth + 1) for key, item in value.items()}
        if isinstance(value, list):
            return [self.convert(item, depth + 1) for item in value]
        return value


def hide_originals(data: bytes, runs: list) -> bytes:
    """Сделать невидимыми операторы, нарисовавшие исходный текст.

    Оператор не вырезается, а обрамляется переключением режима отрисовки:
    `3 Tr` перед ним и возврат прежнего режима после. Вырезать нельзя — сдвиг
    текстовой матрицы, который делает оператор, нужен следующим строкам того же
    блока `BT…ET`.
    """
    ranges: dict[tuple[int, int], int] = {}
    for run in runs:
        if run.in_form or run.op_start < 0 or run.op_end <= run.op_start:
            continue
        ranges[(run.op_start, run.op_end)] = run.restore_render
    if not ranges:
        return data

    out = bytearray()
    cursor = 0
    for (start, end), restore in sorted(ranges.items()):
        if start < cursor or end > len(data):
            continue  # границы наложились друг на друга — целее будет пропустить
        out += data[cursor:start]
        out += b" 3 Tr"
        out += data[start:end]
        out += f" {int(restore)} Tr".encode("ascii")
        cursor = end
    out += data[cursor:]
    return bytes(out)


@dataclass(slots=True)
class Faces:
    """Начертания шрифта, которым печатается перевод.

    Номера объектов выдаются заранее: страницы пишутся по мере обработки, а
    набор использованных глифов известен только в самом конце.
    """

    family: FontFamily
    faces: dict[str, FontFace] = field(default_factory=dict)
    refs: dict[str, Ref] = field(default_factory=dict)

    @staticmethod
    def key_of(bold: bool, italic: bool) -> str:
        return ("b" if bold else "") + ("i" if italic else "") or "r"

    def face(self, pdf: PDFFile, bold: bool, italic: bool) -> FontFace:
        key = self.key_of(bold, italic)
        face = self.faces.get(key)
        if face is None:
            ttf = load(self.family.path_for(bold, italic))
            face = FontFace(
                key=f"PdfTr{key.upper()}", ttf=ttf, ps_name=f"AAAAAA+PdfTr{key.upper()}"
            )
            self.faces[key] = face
            self.refs[key] = pdf.reserve()
        return face

    def finish(self, pdf: PDFFile) -> None:
        for key, face in self.faces.items():
            if not face.used:
                face.used.add(0)  # подмножество без единого глифа не собрать
            face.write(pdf, self.refs[key])


@dataclass(slots=True)
class Typeset:
    """Готовая к печати раскладка перевода внутри рамки абзаца."""

    lines: list[str]
    size: float
    leading: float
    face: FontFace
    fits: bool
    x0: float = 0.0
    width: float = 0.0


def wrap(face: FontFace, text: str, width: float, size: float) -> list[str]:
    """Разбить текст по ширине. Длинное слово при нужде рвётся посимвольно."""
    if width <= 0 or size <= 0:
        return [text]
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if not current or face.measure(candidate, size) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
        while face.measure(current, size) > width and len(current) > 1:
            # Слово шире колонки: отрезаем ровно столько, сколько помещается.
            cut = len(current) - 1
            while cut > 1 and face.measure(current[:cut], size) > width:
                cut -= 1
            lines.append(current[:cut])
            current = current[cut:]
    if current:
        lines.append(current)
    return lines or [""]


def available(block: Block, blocks: list[Block]) -> tuple[float, float]:
    """Левый край и ширина, которыми абзац может распоряжаться.

    У абзаца в несколько строк ширина известна точно — это ширина колонки.
    А вот у строки-одиночки (пункт списка, подпись, ячейка) измеренная ширина
    равна длине самих букв, и держать перевод в этих рамках незачем: справа до
    соседнего блока или до поля страницы обычно есть куда расти. Без этого
    строка из-за более длинного перевода ломалась бы надвое на ровном месте.
    """
    if len(block.lines) > 1:
        return block.x0, block.width

    gap = max(block.size * 0.5, 2.0)
    left = min((other.x0 for other in blocks), default=block.x0)
    right = max((other.x1 for other in blocks), default=block.x1)
    for other in blocks:
        if other is block:
            continue
        if other.bottom >= block.top or other.top <= block.bottom:
            continue  # по вертикали не пересекаются — не мешают
        if other.x0 >= block.x1:
            right = min(right, other.x0 - gap)
        elif other.x1 <= block.x0:
            left = max(left, other.x1 + gap)

    left = min(left, block.x0)
    right = max(right, block.x1)
    if block.align == "right":
        return left, block.x1 - left
    if block.align == "center":
        reach = min(block.x0 - left, right - block.x1)
        return block.x0 - reach, block.width + reach * 2
    return block.x0, right - block.x0


def fit(
    faces: Faces,
    pdf: PDFFile,
    block: Block,
    text: str,
    room: float,
    space: tuple[float, float] | None = None,
) -> Typeset:
    """Подобрать кегль так, чтобы перевод уместился в отведённое место.

    `room` — высота, которой можно распорядиться: своя высота абзаца плюс часть
    пустоты под ним. Кегль уменьшается шагами по 4%, но не ниже разумного:
    нечитаемый перевод хуже слегка выехавшего.
    """
    face = faces.face(pdf, block.bold, _italic_of(block))
    base = max(block.size, MIN_SIZE)
    ratio = min(max((block.leading / base) if base else 1.2, 1.0), 1.9)
    x0, width = space if space is not None else (block.x0, block.width)

    scale = 1.0
    last: Typeset | None = None
    while scale >= MIN_SCALE:
        size = max(base * scale, MIN_SIZE)
        lines = wrap(face, text, width, size)
        leading = max(size * ratio, size * 1.02)
        candidate = Typeset(
            lines, size, leading, face, leading * len(lines) <= room, x0, width
        )
        if candidate.fits or size <= MIN_SIZE:
            return candidate
        last = candidate
        scale -= 0.04
    return last or Typeset([text], base, base * ratio, face, False, x0, width)


def _italic_of(block: Block) -> bool:
    total = sum(len(run.text) for line in block.lines for run in line.runs)
    italic = sum(
        len(run.text) for line in block.lines for run in line.runs if run.font.italic
    )
    return total > 0 and italic * 2 > total


def _from_form(block: Block) -> bool:
    """Текст из Form XObject не сделать невидимым — придётся заклеить фоном."""
    return any(run.in_form for line in block.lines for run in line.runs)


def draw_block(block: Block, plan: Typeset, rtl: bool = False) -> str:
    """Операторы отрисовки одного переведённого абзаца."""
    face = plan.face
    out: list[str] = []
    if block.background is not None or block.over_image or _from_form(block):
        colour = block.background or (1.0, 1.0, 1.0)
        out.append(f"{_colour(colour)} rg")
        for x0, y0, x1, y1 in block.boxes():
            out.append(f"{_n(x0)} {_n(y0)} {_n(x1 - x0)} {_n(y1 - y0)} re f")

    ascent = face.ttf.scale(face.ttf.ascent) / 1000.0
    out.append("BT")
    out.append(f"{_colour(block.color)} rg /{face.key} {_n(plan.size)} Tf")
    top = block.top - plan.size * ascent
    align = "right" if rtl else block.align
    last = len(plan.lines) - 1

    left = plan.x0 or block.x0
    width = plan.width or block.width
    for index, line in enumerate(plan.lines):
        if not line:
            continue
        baseline = top - plan.leading * index
        drawn = face.measure(line, plan.size)
        free = width - drawn
        spacing = 0.0
        x = left
        if align == "right":
            x = left + free
        elif align == "center":
            x = left + free / 2
        elif align == "justify" and index != last and len(line) > 1 and free > 0:
            # Выключка по формату: Tw в Identity-H не работает — пробел там не
            # однобайтовый код 32, — поэтому растягиваем межбуквенным Tc.
            spacing = free / (len(line) - 1)
            if spacing > plan.size * 0.28:
                spacing = 0.0
        out.append(f"{_n(spacing)} Tc")
        out.append(f"1 0 0 1 {_n(x)} {_n(baseline)} Tm <{face.encode(line).hex()}> Tj")
    out.append("0 Tc ET")
    return "\n".join(out)


def room_below(block: Block, blocks: list[Block]) -> float:
    """Сколько места под абзацем можно занять, никого не задев."""
    limit = block.bottom
    for other in blocks:
        if other is block or other.top >= block.top:
            continue
        if other.x1 <= block.x0 + 1 or other.x0 >= block.x1 - 1:
            continue  # не пересекаются по горизонтали — не мешают друг другу
        limit = max(limit, other.top)
    gap = max(0.0, block.bottom - limit)
    return block.height + min(gap * 0.7, block.size * 2.5)


class Builder:
    """Сборка итогового файла: копия исходника плюс перевод."""

    def __init__(self, doc: Document, family: FontFamily, *, title: str = "") -> None:
        self.doc = doc
        self.pdf = PDFFile()
        self.copier = Copier(doc, self.pdf)
        self.faces = Faces(family)
        self.title = title
        self.pages_done = 0
        self.replaced = 0

    def add_page(self, page: Page, blocks: list[Block], rtl: bool = False) -> None:
        """Подготовить страницу: спрятать оригинал и дописать перевод.

        Готовые страницы копятся до `render()` — там они и подставляются вместо
        исходных, потому что копирование дерева страниц идёт разом.
        """
        self.pages_done += 1
        replaced = [
            block for block in blocks
            if block.translatable and block.translated
            and block.translated.strip() != block.text.strip()
        ]
        if not replaced or page.ref is None:
            return

        runs = [run for block in replaced for line in block.lines for run in line.runs]
        content = hide_originals(page.content(), runs)
        drawn = [
            draw_block(
                block,
                fit(
                    self.faces,
                    self.pdf,
                    block,
                    block.translated,
                    room_below(block, blocks),
                    available(block, blocks),
                ),
                rtl,
            )
            for block in replaced
        ]
        shift_x, shift_y, _x1, _y1 = page.box
        content += (
            f"\nq 1 0 0 1 {_n(shift_x)} {_n(shift_y)} cm\n".encode("ascii")
            + "\n".join(drawn).encode("latin-1", "replace")
            + b"\nQ\n"
        )

        info = dict(page.info)
        info["Contents"] = Fixed(self.pdf.add(Stream(content)))
        info["Resources"] = Fixed(self._resources(page))
        self.copier.overrides[page.ref.num] = info
        self.replaced += len(replaced)

    def _resources(self, page: Page) -> dict:
        """Ресурсы страницы плюс наши шрифты, не трогая исходные имена."""
        converted = self.copier.convert(page.resources)
        resources = dict(converted) if isinstance(converted, dict) else {}
        fonts = self.copier.convert(self.doc.resolve(page.resources.get("Font")))
        merged = dict(fonts) if isinstance(fonts, dict) else {}
        for key, face in self.faces.faces.items():
            merged[Name(face.key)] = self.faces.refs[key]
        resources["Font"] = merged
        return resources

    def render(self) -> bytes:
        """Дописать шрифты и собрать файл."""
        self.faces.finish(self.pdf)
        root = self.copier.convert(self.doc.trailer.get("Root"))
        if not isinstance(root, Ref):
            raise ValueError("в исходном файле нет каталога документа")
        info = self.pdf.add(
            {
                "Title": TextString(self.title or "Перевод"),
                "Producer": TextString("pdftr"),
            }
        )
        return self.pdf.build(root, info)
