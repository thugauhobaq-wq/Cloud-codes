"""Исполнение контент-потока страницы: где на листе лежит какой текст.

Контент-поток — это программа: «сдвинь систему координат», «поставь шрифт»,
«нарисуй строку». Координат у строк нет, они получаются из накопленных матриц,
поэтому текст с местом на странице можно получить единственным способом — эту
программу выполнить. Мы выполняем её частично: считаем матрицы, ширины символов
и цвет заливки, а пути и картинки только запоминаем как прямоугольники, чтобы
потом знать, какого цвета фон под абзацем.
"""

from __future__ import annotations

import contextlib
import math
from dataclasses import dataclass, field

from .fonts import Font, page_fonts
from .objects import Name
from .reader import Document, Lexer, Page, StreamObject, _as_number

Matrix = tuple[float, float, float, float, float, float]
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def multiply(first: Matrix, second: Matrix) -> Matrix:
    a1, b1, c1, d1, e1, f1 = first
    a2, b2, c2, d2, e2, f2 = second
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def apply(matrix: Matrix, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    return (a * x + c * y + e, b * x + d * y + f)


@dataclass(slots=True)
class TextRun:
    """Кусок текста, нарисованный одним оператором, уже в координатах страницы."""

    text: str
    x0: float
    y0: float  # базовая линия
    x1: float
    size: float
    font: Font
    color: tuple[float, float, float]
    space_width: float
    angle: float = 0.0
    invisible: bool = False
    # Где в контент-потоке стоит оператор, нарисовавший этот прогон. По этим
    # границам исходный текст потом делается невидимым — вместо заклейки белым.
    op_start: int = -1
    op_end: int = -1
    restore_render: int = 0
    in_form: bool = False

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def top(self) -> float:
        return self.y0 + self.size * (self.font.ascent / 1000.0)

    @property
    def bottom(self) -> float:
        return self.y0 + self.size * (self.font.descent / 1000.0)


@dataclass(slots=True)
class FilledRect:
    """Закрашенный прямоугольник: из него узнаём цвет фона под текстом."""

    x0: float
    y0: float
    x1: float
    y1: float
    color: tuple[float, float, float]

    def area(self) -> float:
        return max(0.0, self.x1 - self.x0) * max(0.0, self.y1 - self.y0)

    def contains(self, x: float, y: float) -> bool:
        return self.x0 - 0.5 <= x <= self.x1 + 0.5 and self.y0 - 0.5 <= y <= self.y1 + 0.5


@dataclass(slots=True)
class PageContent:
    """Результат разбора страницы."""

    runs: list[TextRun] = field(default_factory=list)
    rects: list[FilledRect] = field(default_factory=list)
    images: list[FilledRect] = field(default_factory=list)


@dataclass(slots=True)
class _State:
    ctm: Matrix = IDENTITY
    font: Font | None = None
    size: float = 0.0
    char_spacing: float = 0.0
    word_spacing: float = 0.0
    scale: float = 100.0
    leading: float = 0.0
    rise: float = 0.0
    render: int = 0
    fill: tuple[float, float, float] = (0.0, 0.0, 0.0)
    fill_space: str = "DeviceGray"

    def copy(self) -> _State:
        return _State(
            self.ctm, self.font, self.size, self.char_spacing, self.word_spacing,
            self.scale, self.leading, self.rise, self.render, self.fill, self.fill_space,
        )


def _gray(value: float) -> tuple[float, float, float]:
    value = min(1.0, max(0.0, value))
    return (value, value, value)


def _cmyk(c: float, m: float, y: float, k: float) -> tuple[float, float, float]:
    return (
        min(1.0, max(0.0, (1 - c) * (1 - k))),
        min(1.0, max(0.0, (1 - m) * (1 - k))),
        min(1.0, max(0.0, (1 - y) * (1 - k))),
    )


class Interpreter:
    """Проход по операторам одного потока с полным стеком состояний."""

    MAX_RUNS = 200_000

    def __init__(self, doc: Document, out: PageContent) -> None:
        self.doc = doc
        self.out = out
        self.stack: list[_State] = []
        self.forms: set[int] = set()
        self.in_form = False

    def run(
        self,
        data: bytes,
        resources: dict,
        state: _State,
        depth: int = 0,
    ) -> None:
        if depth > 8 or len(self.out.runs) > self.MAX_RUNS:
            return
        fonts = page_fonts(self.doc, resources)
        lexer = Lexer(data, 0)
        operands: list[object] = []
        text_matrix: Matrix = IDENTITY
        line_matrix: Matrix = IDENTITY
        path: list[tuple[float, float, float, float]] = []
        current = state

        def number(index: int, default: float = 0.0) -> float:
            try:
                value = operands[index]
            except IndexError:
                return default
            return float(value) if isinstance(value, (int, float)) else default

        op_start = lexer.pos
        while True:
            if not operands:
                op_start = lexer.pos
            token = lexer.read_token()
            if token is None:
                break
            if token == b"(":
                operands.append(lexer.read_literal_string())
                continue
            if token == b"<":
                operands.append(lexer.read_hex_string())
                continue
            if token == b"/":
                operands.append(lexer.read_name())
                continue
            if token == b"[":
                items: list[object] = []
                while True:
                    inner = lexer.read_token()
                    if inner is None or inner == b"]":
                        break
                    if inner == b"(":
                        items.append(lexer.read_literal_string())
                    elif inner == b"<":
                        items.append(lexer.read_hex_string())
                    elif inner == b"/":
                        items.append(lexer.read_name())
                    else:
                        items.append(_as_number(inner))
                operands.append(items)
                continue
            if token == b"<<":
                # Словарь среди операндов: параметры gs, BDC, DP. Пропускаем.
                depth_dict = 1
                while depth_dict:
                    inner = lexer.read_token()
                    if inner is None:
                        break
                    if inner == b"<<":
                        depth_dict += 1
                    elif inner == b">>":
                        depth_dict -= 1
                    elif inner == b"(":
                        lexer.read_literal_string()
                    elif inner == b"<":
                        lexer.read_hex_string()
                    elif inner == b"/":
                        lexer.read_name()
                operands.append({})
                continue
            number_value = _as_number(token)
            if number_value is not None:
                operands.append(number_value)
                if len(operands) > 64:
                    del operands[:-32]
                continue

            op = token
            # --- состояние ---
            if op == b"q":
                self.stack.append(current.copy())
            elif op == b"Q":
                if self.stack:
                    current = self.stack.pop()
            elif op == b"cm" and len(operands) >= 6:
                local = (
                    number(-6), number(-5), number(-4), number(-3), number(-2), number(-1),
                )
                current.ctm = multiply(local, current.ctm)
            elif op == b"gs":
                pass  # прозрачность и линии нас не интересуют

            # --- цвет заливки ---
            elif op == b"g" and operands:
                current.fill = _gray(number(-1))
                current.fill_space = "DeviceGray"
            elif op == b"rg" and len(operands) >= 3:
                current.fill = (
                    min(1.0, max(0.0, number(-3))),
                    min(1.0, max(0.0, number(-2))),
                    min(1.0, max(0.0, number(-1))),
                )
                current.fill_space = "DeviceRGB"
            elif op == b"k" and len(operands) >= 4:
                current.fill = _cmyk(number(-4), number(-3), number(-2), number(-1))
                current.fill_space = "DeviceCMYK"
            elif op == b"cs" and operands:
                name = operands[-1]
                current.fill_space = name.value if isinstance(name, Name) else "DeviceRGB"
                current.fill = (0.0, 0.0, 0.0)
            elif op in (b"sc", b"scn"):
                values = [v for v in operands if isinstance(v, (int, float))]
                if len(values) == 1:
                    current.fill = _gray(float(values[0]))
                elif len(values) == 3:
                    current.fill = tuple(min(1.0, max(0.0, float(v))) for v in values)  # type: ignore[assignment]
                elif len(values) == 4:
                    current.fill = _cmyk(*(float(v) for v in values[:4]))

            # --- текст ---
            elif op == b"BT" or op == b"ET":
                text_matrix = line_matrix = IDENTITY
            elif op == b"Tf" and len(operands) >= 2:
                name = operands[-2]
                key = name.value if isinstance(name, Name) else ""
                current.font = fonts.get(key)
                current.size = number(-1)
            elif op == b"Tc":
                current.char_spacing = number(-1)
            elif op == b"Tw":
                current.word_spacing = number(-1)
            elif op == b"Tz":
                current.scale = number(-1, 100.0) or 100.0
            elif op == b"TL":
                current.leading = number(-1)
            elif op == b"Ts":
                current.rise = number(-1)
            elif op == b"Tr":
                current.render = int(number(-1))
            elif op == b"Td" and len(operands) >= 2:
                line_matrix = multiply((1, 0, 0, 1, number(-2), number(-1)), line_matrix)
                text_matrix = line_matrix
            elif op == b"TD" and len(operands) >= 2:
                current.leading = -number(-1)
                line_matrix = multiply((1, 0, 0, 1, number(-2), number(-1)), line_matrix)
                text_matrix = line_matrix
            elif op == b"Tm" and len(operands) >= 6:
                line_matrix = (
                    number(-6), number(-5), number(-4), number(-3), number(-2), number(-1),
                )
                text_matrix = line_matrix
            elif op == b"T*":
                line_matrix = multiply((1, 0, 0, 1, 0, -current.leading), line_matrix)
                text_matrix = line_matrix
            elif op == b"Tj" and operands:
                if isinstance(operands[-1], bytes):
                    mark = len(self.out.runs)
                    text_matrix = self._show(operands[-1], current, text_matrix)
                    self._mark(mark, op_start, lexer.pos, current)
            elif op == b"'" and operands:
                line_matrix = multiply((1, 0, 0, 1, 0, -current.leading), line_matrix)
                text_matrix = line_matrix
                if isinstance(operands[-1], bytes):
                    mark = len(self.out.runs)
                    text_matrix = self._show(operands[-1], current, text_matrix)
                    self._mark(mark, op_start, lexer.pos, current)
            elif op == b'"' and len(operands) >= 3:
                current.word_spacing = number(-3)
                current.char_spacing = number(-2)
                line_matrix = multiply((1, 0, 0, 1, 0, -current.leading), line_matrix)
                text_matrix = line_matrix
                if isinstance(operands[-1], bytes):
                    mark = len(self.out.runs)
                    text_matrix = self._show(operands[-1], current, text_matrix)
                    self._mark(mark, op_start, lexer.pos, current)
            elif op == b"TJ" and operands:
                items = operands[-1]
                if isinstance(items, list):
                    mark = len(self.out.runs)
                    text_matrix = self._show_array(items, current, text_matrix)
                    self._mark(mark, op_start, lexer.pos, current)

            # --- пути: нужны только заливки, и только прямоугольные ---
            elif op == b"re" and len(operands) >= 4:
                x, y, width, height = number(-4), number(-3), number(-2), number(-1)
                path.append((x, y, x + width, y + height))
            elif op in (b"m", b"l", b"c", b"v", b"y", b"h"):
                pass
            elif op in (b"f", b"F", b"f*", b"b", b"b*", b"B", b"B*"):
                self._fill(path, current)
                path = []
            elif op in (b"S", b"s", b"n"):
                path = []
            elif op == b"W" or op == b"W*":
                pass

            # --- вложенные объекты ---
            elif op == b"Do" and operands:
                name = operands[-1]
                if isinstance(name, Name):
                    self._xobject(name.value, resources, current, depth)
            elif op == b"BI":
                self._skip_inline_image(lexer, current)

            operands = []
        state.ctm = current.ctm

    def _mark(self, first: int, start: int, end: int, state: _State) -> None:
        """Приписать свежим прогонам границы оператора, который их нарисовал."""
        for run in self.out.runs[first:]:
            run.op_start = start
            run.op_end = end
            run.restore_render = state.render
            run.in_form = self.in_form

    # --- отрисовка текста ---------------------------------------------------

    def _show_array(self, items: list, state: _State, text_matrix: Matrix) -> Matrix:
        """Оператор TJ: строки вперемешку со сдвигами в тысячных долях кегля."""
        for item in items:
            if isinstance(item, bytes):
                text_matrix = self._show(item, state, text_matrix)
            elif isinstance(item, (int, float)):
                shift = -float(item) / 1000.0 * state.size * (state.scale / 100.0)
                text_matrix = multiply((1, 0, 0, 1, shift, 0), text_matrix)
        return text_matrix

    def _show(self, raw: bytes, state: _State, text_matrix: Matrix) -> Matrix:
        font = state.font
        if font is None or not raw:
            return text_matrix
        horizontal = state.scale / 100.0
        pieces: list[str] = []
        advance = 0.0
        for code, char, width in font.decode(raw):
            pieces.append(char)
            step = width / 1000.0 * state.size + state.char_spacing
            if code == 32 and not font.two_byte:
                step += state.word_spacing
            advance += step * horizontal
        text = "".join(pieces)

        start = multiply(
            (state.size * horizontal, 0, 0, state.size, 0, state.rise), text_matrix
        )
        device = multiply(start, state.ctm)
        x0, y0 = device[4], device[5]
        end_matrix = multiply((1, 0, 0, 1, advance, 0), text_matrix)
        x1, y1 = apply(state.ctm, end_matrix[4], end_matrix[5])

        if text.strip() and len(self.out.runs) < self.MAX_RUNS:
            a, b, c, d = device[0], device[1], device[2], device[3]
            size = math.hypot(c, d) or math.hypot(a, b) or state.size
            angle = math.degrees(math.atan2(b, a))
            space = font.width(32) / 1000.0 * size * horizontal
            self.out.runs.append(
                TextRun(
                    text=text,
                    x0=x0,
                    y0=y0,
                    x1=x1 if abs(angle) < 1 else x0 + math.hypot(x1 - x0, y1 - y0),
                    size=size,
                    font=font,
                    color=state.fill,
                    space_width=space or size * 0.25,
                    angle=angle,
                    invisible=state.render == 3 or state.render == 7,
                )
            )
        return end_matrix

    # --- прочее -------------------------------------------------------------

    def _fill(self, path: list[tuple[float, float, float, float]], state: _State) -> None:
        for x0, y0, x1, y1 in path[-16:]:
            corners = [
                apply(state.ctm, x0, y0),
                apply(state.ctm, x1, y0),
                apply(state.ctm, x1, y1),
                apply(state.ctm, x0, y1),
            ]
            xs = [point[0] for point in corners]
            ys = [point[1] for point in corners]
            rect = FilledRect(min(xs), min(ys), max(xs), max(ys), state.fill)
            if rect.area() > 4:
                self.out.rects.append(rect)
                if len(self.out.rects) > 20000:
                    del self.out.rects[:10000]

    def _xobject(self, key: str, resources: dict, state: _State, depth: int) -> None:
        table = self.doc.resolve(resources.get("XObject"))
        if not isinstance(table, dict):
            return
        target = table.get(key)
        stream = self.doc.resolve(target)
        if not isinstance(stream, StreamObject):
            return
        subtype = self.doc.resolve(stream.info.get("Subtype"))
        subtype = subtype.value if isinstance(subtype, Name) else ""
        if subtype == "Image":
            corners = [
                apply(state.ctm, 0, 0), apply(state.ctm, 1, 0),
                apply(state.ctm, 1, 1), apply(state.ctm, 0, 1),
            ]
            xs = [point[0] for point in corners]
            ys = [point[1] for point in corners]
            self.out.images.append(
                FilledRect(min(xs), min(ys), max(xs), max(ys), (1.0, 1.0, 1.0))
            )
            return
        if subtype != "Form":
            return
        marker = id(stream)
        if marker in self.forms:
            return  # форма вставляет сама себя — дальше не идём
        self.forms.add(marker)
        outer_form = self.in_form
        self.in_form = True
        inner = state.copy()
        matrix = self.doc.resolve(stream.info.get("Matrix"))
        if isinstance(matrix, list) and len(matrix) >= 6:
            values = []
            for item in matrix[:6]:
                item = self.doc.resolve(item)
                values.append(float(item) if isinstance(item, (int, float)) else 0.0)
            inner.ctm = multiply(tuple(values), inner.ctm)  # type: ignore[arg-type]
        own = self.doc.resolve(stream.info.get("Resources"))
        self.run(
            stream.decoded(),
            own if isinstance(own, dict) else resources,
            inner,
            depth + 1,
        )
        self.in_form = outer_form
        self.forms.discard(marker)

    @staticmethod
    def _skip_inline_image(lexer: Lexer, state: _State) -> None:
        """Пропустить BI…ID…EI: между ID и EI лежат сырые пиксели."""
        data = lexer.data
        marker = data.find(b"ID", lexer.pos)
        if marker < 0:
            lexer.pos = lexer.size
            return
        cursor = marker + 3
        while True:
            end = data.find(b"EI", cursor)
            if end < 0:
                lexer.pos = lexer.size
                return
            before = data[end - 1] if end else 0x20
            after = data[end + 2] if end + 2 < len(data) else 0x20
            if before in b"\x00\t\n\x0c\r " and after in b"\x00\t\n\x0c\r []/<":
                lexer.pos = end + 2
                return
            cursor = end + 2


def read_page(page: Page) -> PageContent:
    """Разобрать страницу: текст с координатами, заливки и рамки картинок."""
    out = PageContent()
    interpreter = Interpreter(page.doc, out)
    state = _State()
    x0, y0, _x1, _y1 = page.box
    if x0 or y0:
        # Начало координат страницы может быть сдвинуто — приводим к нулю.
        state.ctm = (1.0, 0.0, 0.0, 1.0, -x0, -y0)
    # Половина разобранной страницы лучше, чем упавшая обработка документа.
    with contextlib.suppress(ValueError, TypeError, IndexError, KeyError, RecursionError):
        interpreter.run(page.content(), page.resources, state)
    return out
