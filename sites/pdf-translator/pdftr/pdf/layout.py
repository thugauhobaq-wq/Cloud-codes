"""Из россыпи текстовых прогонов — строки, абзацы и колонки.

В PDF нет ни абзацев, ни даже строк: есть «нарисуй эти буквы вот здесь».
Переводить по прогонам нельзя — в них попадают куски слов, а переводчику нужна
законченная мысль. Поэтому прогоны собираются обратно: сначала в строки по
общей базовой линии, потом в абзацы по вертикальным промежуткам и отступам.

Колонки получаются сами собой: абзац растёт только из строк, которые
пересекаются с ним по горизонтали, а строки соседней колонки не пересекаются
ни с одной строкой первой. Так текст из левой колонки не склеивается с правой.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

from .content import FilledRect, PageContent, TextRun

# Символы, с которых начинается пункт списка. Такая строка всегда открывает
# новый абзац, даже если промежуток до предыдущей строки обычный.
_BULLET = re.compile(r"^\s*(?:[•▪◦‣·∙⁃–—*]|\(?\d{1,3}[.)]|[a-zA-Zа-яА-Я][.)])\s+")
_LETTERS = re.compile(r"[^\W\d_]", re.UNICODE)


@dataclass(slots=True)
class Line:
    """Строка: прогоны с одной базовой линией, слева направо."""

    runs: list[TextRun]
    text: str
    x0: float
    x1: float
    y: float
    size: float

    @property
    def top(self) -> float:
        return max(run.top for run in self.runs)

    @property
    def bottom(self) -> float:
        return min(run.bottom for run in self.runs)

    @property
    def bold(self) -> bool:
        return sum(len(run.text) for run in self.runs if run.font.bold) * 2 > len(self.text)

    @property
    def color(self) -> tuple[float, float, float]:
        counts: dict[tuple[float, float, float], int] = {}
        for run in self.runs:
            counts[run.color] = counts.get(run.color, 0) + len(run.text)
        return max(counts, key=counts.get) if counts else (0.0, 0.0, 0.0)

    @property
    def invisible(self) -> bool:
        return all(run.invisible for run in self.runs)


@dataclass(slots=True)
class Block:
    """Абзац: то, что уходит переводчику одним куском."""

    lines: list[Line]
    text: str = ""
    size: float = 0.0
    leading: float = 0.0
    align: str = "left"
    bold: bool = False
    color: tuple[float, float, float] = (0.0, 0.0, 0.0)
    background: tuple[float, float, float] | None = None
    over_image: bool = False
    invisible: bool = False
    translatable: bool = True
    translated: str = ""

    @property
    def x0(self) -> float:
        return min(line.x0 for line in self.lines)

    @property
    def x1(self) -> float:
        return max(line.x1 for line in self.lines)

    @property
    def top(self) -> float:
        return max(line.top for line in self.lines)

    @property
    def bottom(self) -> float:
        return min(line.bottom for line in self.lines)

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.top - self.bottom

    def boxes(self) -> list[tuple[float, float, float, float]]:
        """Прямоугольники под заклейку — по строкам, а не общей рамкой.

        Общая рамка у абзаца в две колонки таблицы накрыла бы соседнюю ячейку,
        поэтому закрашиваем ровно то, что занято буквами.
        """
        out = []
        for line in self.lines:
            pad = line.size * 0.12
            out.append(
                (line.x0 - pad, line.bottom - pad * 0.5, line.x1 + pad, line.top + pad * 0.3)
            )
        return out


def _merge_line_text(runs: list[TextRun]) -> str:
    """Склеить прогоны строки, вернув пробелы, которых нет в потоке."""
    parts: list[str] = []
    previous: TextRun | None = None
    for run in runs:
        if previous is not None:
            gap = run.x0 - previous.x1
            threshold = max(previous.space_width, run.space_width) * 0.28
            if gap > threshold and not parts[-1].endswith(" ") and not run.text.startswith(" "):
                parts.append(" ")
            elif gap > max(previous.space_width, run.space_width) * 2.5:
                parts.append("  ")  # разрыв на всю ширину: скорее всего колонка таблицы
        parts.append(run.text)
        previous = run
    return re.sub(r"[ \t]{3,}", "  ", "".join(parts)).strip()


def _empty_bands(
    intervals: list[tuple[float, float]], minimum: float
) -> list[tuple[float, float]]:
    """Промежутки, где нет ни одного отрезка, шириной не меньше `minimum`.

    Проекция всего текста на ось: по оси X пустоты — это межколонник, по оси Y —
    отбивки между блоками. На краях промежутки не считаются: это поля страницы.
    """
    if len(intervals) < 2:
        return []
    ordered = sorted(intervals)
    gaps: list[tuple[float, float]] = []
    reach = ordered[0][1]
    for start, end in ordered[1:]:
        if start - reach >= minimum:
            gaps.append((reach, start))
        reach = max(reach, end)
    return gaps


def _cut(runs: list[TextRun], depth: int = 0) -> list[list[TextRun]]:
    """Рекурсивно разрезать страницу пустотами: сначала вдоль, потом поперёк.

    Классический XY-cut. Он и разводит колонки: заголовок во всю ширину не даёт
    разрезать страницу вертикально, зато отбивка под ним разрезает горизонтально,
    а уже в нижней части межколонник виден насквозь.
    """
    if len(runs) < 4 or depth > 14:
        return [runs]
    typical = statistics.median([run.size for run in runs]) or 10.0

    columns = _empty_bands(
        [(run.x0, run.x1) for run in runs], max(9.0, typical * 1.1)
    )
    if columns:
        edge = max(columns, key=lambda band: band[1] - band[0])[0]
        left = [run for run in runs if run.x1 <= edge]
        right = [run for run in runs if run.x1 > edge]
        if left and right:
            return _cut(left, depth + 1) + _cut(right, depth + 1)

    # Горизонтально режем по самой заметной пустоте, а не по любой: между
    # строками одного абзаца просвет тоже есть, и резать по нему нечего. Отбивка
    # между блоками отличается тем, что она заметно больше обычного просвета в
    # этом же куске страницы, — на это и смотрим.
    rows = _empty_bands([(run.bottom, run.top) for run in runs], 0.5)
    if rows:
        heights = sorted(band[1] - band[0] for band in rows)
        usual = statistics.median(heights)
        widest = max(rows, key=lambda band: band[1] - band[0])
        span = widest[1] - widest[0]
        if span >= typical * 0.5 and span >= usual * 2.2:
            edge = widest[1]
            upper = [run for run in runs if run.bottom >= edge]
            lower = [run for run in runs if run.bottom < edge]
            if upper and lower:
                return _cut(upper, depth + 1) + _cut(lower, depth + 1)
    return [runs]


def regions(runs: list[TextRun]) -> list[list[TextRun]]:
    """Прогоны страницы → куски в порядке чтения: колонка за колонкой."""
    upright = [
        run for run in runs
        if abs(run.angle) < 3.0 and run.text.strip() and run.size > 0.5
    ]
    if not upright:
        return []
    parts = [part for part in _cut(upright) if part]
    parts.sort(key=lambda part: (-max(run.top for run in part), min(run.x0 for run in part)))
    return parts


def build_lines(runs: list[TextRun]) -> list[Line]:
    """Собрать прогоны в строки по общей базовой линии."""
    upright = [
        run for run in runs
        if abs(run.angle) < 3.0 and run.text.strip() and run.size > 0.5
    ]
    if not upright:
        return []
    upright.sort(key=lambda run: (-run.y0, run.x0))

    lines: list[Line] = []
    bucket: list[TextRun] = []
    baseline = upright[0].y0
    height = upright[0].size

    for run in upright:
        tolerance = max(height, run.size) * 0.34
        if bucket and abs(run.y0 - baseline) > tolerance:
            lines.append(_close_line(bucket))
            bucket = []
        if not bucket:
            baseline = run.y0
            height = run.size
        else:
            # Базовая линия строки — по самому крупному прогону: индексы и
            # сноски не должны утаскивать её вверх или вниз.
            if run.size > height:
                baseline, height = run.y0, run.size
        bucket.append(run)
    if bucket:
        lines.append(_close_line(bucket))
    return [line for line in lines if line.text]


def _close_line(bucket: list[TextRun]) -> Line:
    ordered = sorted(bucket, key=lambda run: run.x0)
    sizes = [run.size for run in ordered]
    return Line(
        runs=ordered,
        text=_merge_line_text(ordered),
        x0=min(run.x0 for run in ordered),
        x1=max(run.x1 for run in ordered),
        y=statistics.median([run.y0 for run in ordered]),
        size=max(sizes),
    )


def _overlap(first: Line, second: Line) -> float:
    """Доля пересечения по горизонтали от более узкой строки."""
    left = max(first.x0, second.x0)
    right = min(first.x1, second.x1)
    if right <= left:
        return 0.0
    narrow = min(first.x1 - first.x0, second.x1 - second.x0)
    return (right - left) / narrow if narrow > 0 else 0.0


def build_blocks(lines: list[Line]) -> list[Block]:
    """Строки → абзацы. Новый абзац начинают отступ, промежуток или другой кегль."""
    blocks: list[Block] = []
    current: list[Line] = []

    def flush() -> None:
        if current:
            blocks.append(_close_block(list(current)))
            current.clear()

    for line in lines:
        if not current:
            current.append(line)
            continue
        previous = current[-1]
        gap = previous.y - line.y
        typical = max(previous.size, line.size)
        # Ожидаемый межстрочный интервал: либо уже накопленный в абзаце, либо
        # 1.2 кегля — так отличается перенос строки от нового абзаца.
        expected = _leading_of(current) or typical * 1.2

        broken = (
            gap > expected * 1.55
            or gap < typical * 0.35  # строки налезли друг на друга — разные объекты
            or _overlap(previous, line) < 0.12
            or abs(previous.size - line.size) > typical * 0.28
            or previous.bold != line.bold
            or previous.color != line.color
            or _BULLET.match(line.text)
            or _ends_paragraph(previous, current, line)
        )
        if broken:
            flush()
        current.append(line)
    flush()
    return blocks


def _leading_of(lines: list[Line]) -> float:
    if len(lines) < 2:
        return 0.0
    steps = [lines[index - 1].y - lines[index].y for index in range(1, len(lines))]
    steps = [step for step in steps if step > 0]
    return statistics.median(steps) if steps else 0.0


def _ends_paragraph(previous: Line, block: list[Line], nxt: Line) -> bool:
    """Короткая последняя строка плюс отступ у следующей — граница абзаца."""
    right = max(line.x1 for line in block)
    left = min(line.x0 for line in block)
    short = previous.x1 < right - previous.size * 2.0
    indented = nxt.x0 > left + nxt.size * 0.8
    return short and indented


def _close_block(lines: list[Line]) -> Block:
    sizes = [line.size for line in lines]
    block = Block(lines=lines)
    block.size = statistics.median(sizes)
    block.leading = _leading_of(lines) or block.size * 1.2
    block.bold = sum(1 for line in lines if line.bold) * 2 > len(lines)
    block.color = statistics.mode([line.color for line in lines])
    block.invisible = all(line.invisible for line in lines)
    block.text = _join_lines(lines)
    block.align = _alignment(lines)
    block.translatable = bool(_LETTERS.search(block.text)) and any(
        run.font.reliable for line in lines for run in line.runs
    )
    return block


def _join_lines(lines: list[Line]) -> str:
    """Строки абзаца → один текст, со снятыми переносами по слогам."""
    out = ""
    for index, line in enumerate(lines):
        text = line.text
        if index == 0:
            out = text
            continue
        if out.endswith(("-", "­", "‐")) and text[:1].islower():
            out = out[:-1] + text  # перенос слова: дефис уходит
        else:
            out += " " + text
    return out.strip()


def _alignment(lines: list[Line]) -> str:
    """По разбросу краёв понять, как абзац выключен."""
    if len(lines) < 2:
        return "left"
    lefts = [line.x0 for line in lines]
    body = lines[:-1]  # последняя строка абзаца короче по определению
    left_spread = max(lefts) - min(lefts)
    right_spread = max(line.x1 for line in body) - min(line.x1 for line in body)
    size = statistics.median([line.size for line in lines])
    if left_spread < size * 0.5 and right_spread < size * 0.5:
        return "justify"
    if left_spread < size * 0.5:
        return "left"
    if right_spread < size * 0.5:
        return "right"
    centers = [(line.x0 + line.x1) / 2 for line in lines]
    if max(centers) - min(centers) < size * 0.6:
        return "center"
    return "left"


def _background_for(block: Block, rects: list[FilledRect]) -> tuple[float, float, float] | None:
    """Цвет заливки под абзацем, если он лежит на плашке."""
    x = (block.x0 + block.x1) / 2
    y = (block.top + block.bottom) / 2
    best: FilledRect | None = None
    for rect in rects:
        if not rect.contains(x, y):
            continue
        if rect.area() < block.width * max(block.height, 1.0) * 0.4:
            continue
        if best is None or rect.area() <= best.area():
            best = rect
    if best is None:
        return None
    if all(channel > 0.985 for channel in best.color):
        return None  # белая плашка — от неё ничего не меняется
    return best.color


def _over_image(block: Block, images: list[FilledRect]) -> bool:
    x = (block.x0 + block.x1) / 2
    y = (block.top + block.bottom) / 2
    return any(image.contains(x, y) for image in images)


def analyse(content: PageContent) -> list[Block]:
    """Полный разбор страницы: прогоны → абзацы с фоном и признаками."""
    blocks: list[Block] = []
    for part in regions(content.runs):
        blocks.extend(build_blocks(build_lines(part)))
    rects = sorted(content.rects, key=lambda rect: rect.area())
    for block in blocks:
        block.background = _background_for(block, rects)
        block.over_image = _over_image(block, content.images)
        if block.invisible and not block.over_image:
            # Невидимый текст не поверх картинки — служебная разметка, не трогаем.
            block.translatable = False
    return blocks
