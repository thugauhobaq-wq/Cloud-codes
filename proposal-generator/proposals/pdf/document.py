"""Холст и документ: то, чем пользуется вёрстка предложения.

Координаты здесь человеческие — начало в левом верхнем углу, `y` растёт вниз,
как в вебе. В сам PDF всё переворачивается при записи: заниматься этим при
каждом вызове `text()` невозможно, ошибёшься на второй странице.

Единица измерения — типографский пункт (1/72 дюйма), как и внутри PDF.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .fonts import FontError, FontFace, FontFamily, load
from .images import RasterImage, load_image
from .objects import Name, PDFFile, Ref, Stream, TextString, fmt_number

MM = 72.0 / 25.4
A4 = (210 * MM, 297 * MM)

Color = tuple[float, float, float]

REGULAR = "regular"
BOLD = "bold"
ITALIC = "italic"
BOLD_ITALIC = "bolditalic"


def rgb(value: str | Color) -> Color:
    """`#1f6feb`, `#fff` или готовый кортеж → доли единицы для PDF."""
    if isinstance(value, tuple):
        return value
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    if len(text) != 6:
        raise ValueError(f"непонятный цвет: {value!r}")
    try:
        number = int(text, 16)
    except ValueError as error:
        raise ValueError(f"непонятный цвет: {value!r}") from error
    return ((number >> 16 & 255) / 255, (number >> 8 & 255) / 255, (number & 255) / 255)


def mix(first: Color, second: Color, ratio: float) -> Color:
    """Смешать цвета: `ratio=0` — первый, `1` — второй."""
    return tuple(a + (b - a) * ratio for a, b in zip(first, second, strict=True))  # type: ignore[return-value]


def readable_on(background: Color) -> Color:
    """Чёрный или белый — смотря что читается на подложке.

    Коэффициенты яркости из BT.601: акцентный цвет задаёт клиент, и на жёлтом
    белый текст пропадает.
    """
    luma = 0.299 * background[0] + 0.587 * background[1] + 0.114 * background[2]
    return (0.0, 0.0, 0.0) if luma > 0.6 else (1.0, 1.0, 1.0)


@dataclass(slots=True)
class Image:
    """Картинка, уже положенная в документ."""

    key: str
    raster: RasterImage

    @property
    def aspect(self) -> float:
        return self.raster.aspect


class Page:
    """Содержимое одной страницы: список операторов рисования."""

    def __init__(self, document: Document, width: float, height: float) -> None:
        self.document = document
        self.width = width
        self.height = height
        self._ops: list[str] = []
        self.used_images: set[str] = set()

    # --- служебное -----------------------------------------------------------

    def _write(self, line: str) -> None:
        self._ops.append(line)

    def _flip(self, y: float) -> float:
        return self.height - y

    def content(self) -> bytes:
        return "\n".join(self._ops).encode("latin-1")

    # --- фигуры --------------------------------------------------------------

    def rect(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        *,
        fill: Color | None = None,
        stroke: Color | None = None,
        line_width: float = 0.6,
    ) -> None:
        if fill is None and stroke is None:
            return
        parts = []
        if fill is not None:
            parts.append(f"{_c(fill)} rg")
        if stroke is not None:
            parts.append(f"{_c(stroke)} RG {fmt_number(line_width)} w")
        operator = "B" if (fill is not None and stroke is not None) else ("f" if fill else "S")
        box = f"{_n(x)} {_n(self._flip(y) - height)} {_n(width)} {_n(height)} re"
        self._write(f"q {' '.join(parts)} {box} {operator} Q")

    def round_rect(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        radius: float,
        *,
        fill: Color | None = None,
        stroke: Color | None = None,
        line_width: float = 0.6,
    ) -> None:
        radius = min(radius, width / 2, height / 2)
        if radius <= 0:
            self.rect(x, y, width, height, fill=fill, stroke=stroke, line_width=line_width)
            return
        # Магическая константа Безье: 4/3·(√2−1) — дуга в четверть окружности.
        k = radius * 0.5523
        bottom = self._flip(y) - height
        top = self._flip(y)
        left, right = x, x + width
        path = [
            f"{_n(left + radius)} {_n(bottom)} m",
            f"{_n(right - radius)} {_n(bottom)} l",
            f"{_n(right - radius + k)} {_n(bottom)} {_n(right)} {_n(bottom + radius - k)}"
            f" {_n(right)} {_n(bottom + radius)} c",
            f"{_n(right)} {_n(top - radius)} l",
            f"{_n(right)} {_n(top - radius + k)} {_n(right - radius + k)} {_n(top)}"
            f" {_n(right - radius)} {_n(top)} c",
            f"{_n(left + radius)} {_n(top)} l",
            f"{_n(left + radius - k)} {_n(top)} {_n(left)} {_n(top - radius + k)}"
            f" {_n(left)} {_n(top - radius)} c",
            f"{_n(left)} {_n(bottom + radius)} l",
            f"{_n(left)} {_n(bottom + radius - k)} {_n(left + radius - k)} {_n(bottom)}"
            f" {_n(left + radius)} {_n(bottom)} c",
            "h",
        ]
        parts = []
        if fill is not None:
            parts.append(f"{_c(fill)} rg")
        if stroke is not None:
            parts.append(f"{_c(stroke)} RG {fmt_number(line_width)} w")
        operator = "B" if (fill is not None and stroke is not None) else ("f" if fill else "S")
        self._write(f"q {' '.join(parts)} {' '.join(path)} {operator} Q")

    def line(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        *,
        color: Color = (0.8, 0.8, 0.8),
        width: float = 0.6,
        dash: tuple[float, float] | None = None,
    ) -> None:
        pattern = f"[{_n(dash[0])} {_n(dash[1])}] 0 d " if dash else ""
        self._write(
            f"q {_c(color)} RG {fmt_number(width)} w {pattern}"
            f"{_n(x1)} {_n(self._flip(y1))} m {_n(x2)} {_n(self._flip(y2))} l S Q"
        )

    # --- текст ---------------------------------------------------------------

    def text(
        self,
        x: float,
        y: float,
        text: str,
        *,
        size: float = 10,
        style: str = REGULAR,
        color: Color = (0, 0, 0),
        align: str = "left",
        width: float | None = None,
        char_spacing: float = 0.0,
    ) -> float:
        """Одна строка. `y` — верх строки; возвращает `y` под ней.

        При `align="right"`/`"center"` нужен `width` — ширина колонки, внутри
        которой выравниваем.
        """
        if not text:
            return y + self.document.line_height(size)
        face = self.document.face(style)
        drawn = face.measure(text, size) + char_spacing * max(len(text) - 1, 0)
        if align == "right" and width is not None:
            x = x + width - drawn
        elif align == "center" and width is not None:
            x = x + (width - drawn) / 2

        baseline = self._flip(y + self.document.ascent(size))
        # Tc пишем всегда, даже нулевой: параметры текста живут до конца потока,
        # и разрядка одного заголовка иначе расползётся по всей странице.
        spacing = f"{fmt_number(char_spacing)} Tc "
        self._write(
            f"BT {_c(color)} rg /{face.key} {fmt_number(size)} Tf {spacing}"
            f"1 0 0 1 {_n(x)} {_n(baseline)} Tm <{face.encode(text).hex()}> Tj ET"
        )
        return y + self.document.line_height(size)

    def paragraph(
        self,
        x: float,
        y: float,
        width: float,
        text: str,
        *,
        size: float = 10,
        style: str = REGULAR,
        color: Color = (0, 0, 0),
        leading: float = 1.35,
        align: str = "left",
    ) -> float:
        """Абзац с переносами. Возвращает `y` под последней строкой."""
        lines = self.document.wrap(text, width, size=size, style=style)
        step = size * leading
        for index, line in enumerate(lines):
            self.text(
                x,
                y + index * step,
                line,
                size=size,
                style=style,
                color=color,
                align=align,
                width=width,
            )
        return y + max(len(lines), 1) * step

    # --- картинки ------------------------------------------------------------

    def image(self, image: Image, x: float, y: float, width: float, height: float) -> None:
        self.used_images.add(image.key)
        bottom = self._flip(y) - height
        self._write(
            f"q {_n(width)} 0 0 {_n(height)} {_n(x)} {_n(bottom)} cm /{image.key} Do Q"
        )

    def image_fit(
        self,
        image: Image,
        x: float,
        y: float,
        box_width: float,
        box_height: float,
        *,
        align: str = "left",
    ) -> tuple[float, float]:
        """Вписать картинку в прямоугольник, не искажая пропорции.

        Возвращает фактический размер — логотип 3:1 и логотип-квадрат занимают
        разное место, а подпись под ними должна встать вплотную.
        """
        width = box_width
        height = width / image.aspect
        if height > box_height:
            height = box_height
            width = height * image.aspect
        offset = {"left": 0.0, "center": (box_width - width) / 2, "right": box_width - width}[align]
        self.image(image, x + offset, y + (box_height - height) / 2, width, height)
        return width, height

def _n(value: float) -> str:
    return fmt_number(value)


def _c(color: Color) -> str:
    return " ".join(fmt_number(round(channel, 4)) for channel in color)


class Document:
    """Набор страниц, шрифтов и картинок."""

    def __init__(
        self,
        family: FontFamily,
        *,
        size: tuple[float, float] = A4,
        title: str = "",
        author: str = "",
        subject: str = "",
    ) -> None:
        self.family = family
        self.size = size
        self.title = title
        self.author = author
        self.subject = subject
        self.pages: list[Page] = []
        self._faces: dict[str, FontFace] = {}
        self._images: dict[str, Image] = {}
        self.missing_glyphs: set[str] = set()

    # --- шрифты --------------------------------------------------------------

    def face(self, style: str = REGULAR) -> FontFace:
        bold = style in (BOLD, BOLD_ITALIC)
        italic = style in (ITALIC, BOLD_ITALIC)
        path = self.family.path_for(bold, italic)
        # Если нужного начертания в системе нет, path_for отдаст обычное —
        # тогда и ключ шрифта должен совпасть, иначе положим файл дважды.
        key = f"F{abs(hash(str(path))) % 100000}"
        existing = self._faces.get(key)
        if existing is None:
            ttf = load(path)
            if not ttf.has_cmap:
                raise FontError(f"{path}: в шрифте нет таблицы cmap, символы не с чем сопоставить")
            suffix = {REGULAR: "", BOLD: "Bold", ITALIC: "Italic", BOLD_ITALIC: "BoldItalic"}[style]
            base = "".join(char for char in self.family.name if char.isalnum()) or "Font"
            existing = FontFace(key=key, ttf=ttf, ps_name=f"{base}{suffix}")
            self._faces[key] = existing
        return existing

    def ascent(self, size: float) -> float:
        ttf = self.face(REGULAR).ttf
        return ttf.scale(ttf.ascent) / 1000.0 * size

    def line_height(self, size: float) -> float:
        ttf = self.face(REGULAR).ttf
        return ttf.scale(ttf.ascent - ttf.descent) / 1000.0 * size

    def measure(self, text: str, *, size: float = 10, style: str = REGULAR) -> float:
        return self.face(style).measure(text, size)

    def check_glyphs(self, text: str, style: str = REGULAR) -> None:
        """Запомнить символы, которых нет в шрифте, чтобы предупредить в конце."""
        self.missing_glyphs |= self.face(style).missing(text)

    def wrap(self, text: str, width: float, *, size: float = 10, style: str = REGULAR) -> list[str]:
        """Разбить текст по ширине. Явные переводы строки сохраняются."""
        face = self.face(style)
        lines: list[str] = []
        for chunk in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            words = chunk.split()
            if not words:
                lines.append("")
                continue
            current = ""
            for word in words:
                candidate = f"{current} {word}" if current else word
                if face.measure(candidate, size) <= width or not current:
                    current = candidate
                else:
                    lines.append(current)
                    current = word
                # Слово длиннее строки (артикул, ссылка) — рвём по символам.
                while face.measure(current, size) > width and len(current) > 1:
                    cut = len(current) - 1
                    while cut > 1 and face.measure(current[:cut], size) > width:
                        cut -= 1
                    lines.append(current[:cut])
                    current = current[cut:]
            lines.append(current)
        return lines

    def ellipsize(self, text: str, width: float, *, size: float = 10, style: str = REGULAR) -> str:
        """Обрезать строку до ширины, поставив многоточие."""
        face = self.face(style)
        if face.measure(text, size) <= width:
            return text
        cut = len(text)
        while cut > 1 and face.measure(text[:cut] + "…", size) > width:
            cut -= 1
        return text[:cut].rstrip() + "…"

    def text_height(self, text: str, width: float, *, size: float = 10, leading: float = 1.35,
                    style: str = REGULAR) -> float:
        """Сколько займёт абзац — нужно, чтобы решить, влезет ли он на страницу."""
        return max(len(self.wrap(text, width, size=size, style=style)), 1) * size * leading

    # --- картинки ------------------------------------------------------------

    def add_image(self, data: bytes) -> Image:
        """Положить картинку в документ; одинаковые файлы хранятся один раз."""
        digest = hashlib.sha1(data).hexdigest()[:12]
        existing = self._images.get(digest)
        if existing is None:
            existing = Image(key=f"Im{digest}", raster=load_image(data))
            self._images[digest] = existing
        return existing

    # --- страницы ------------------------------------------------------------

    def new_page(self) -> Page:
        page = Page(self, *self.size)
        self.pages.append(page)
        return page

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def render(self) -> bytes:
        """Собрать PDF. После вызова документ трогать больше нельзя."""
        if not self.pages:
            self.new_page()

        pdf = PDFFile()
        pages_ref = pdf.reserve()

        font_refs = {face.key: face.write(pdf) for face in self._faces.values()}
        # Картинку могли добавить, но так и не нарисовать — например, шаблон
        # решил обойтись без логотипа. В файл такие не кладём.
        drawn = {key for page in self.pages for key in page.used_images}
        image_refs = {
            image.key: image.raster.write(pdf)
            for image in self._images.values()
            if image.key in drawn
        }

        kids: list[Ref] = []
        for page in self.pages:
            content = pdf.add(Stream(page.content()))
            resources: dict[str, object] = {
                "Font": {Name(key): ref for key, ref in font_refs.items()}
            }
            if page.used_images:
                resources["XObject"] = {
                    Name(key): image_refs[key] for key in sorted(page.used_images)
                }
            kids.append(
                pdf.add(
                    {
                        "Type": Name("Page"),
                        "Parent": pages_ref,
                        "MediaBox": [0, 0, page.width, page.height],
                        "Resources": resources,
                        "Contents": content,
                    }
                )
            )

        pdf.assign(
            pages_ref,
            {"Type": Name("Pages"), "Kids": kids, "Count": len(kids)},
        )
        catalog = pdf.add(
            {
                "Type": Name("Catalog"),
                "Pages": pages_ref,
                "ViewerPreferences": {"DisplayDocTitle": True},
            }
        )
        info: dict[str, object] = {"Producer": TextString("proposals — генератор КП")}
        if self.title:
            info["Title"] = TextString(self.title)
        if self.author:
            info["Author"] = TextString(self.author)
        if self.subject:
            info["Subject"] = TextString(self.subject)
        info_ref = pdf.add(info)

        # Идентификатор файла делаем от содержимого: одинаковый вход — одинаковый
        # PDF, это заметно упрощает тесты и сравнение версий предложения.
        seed = hashlib.md5(
            b"".join(page.content() for page in self.pages) + self.title.encode()
        ).digest()
        return pdf.build(catalog, info_ref, doc_id=seed)
