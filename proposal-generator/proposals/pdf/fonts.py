"""Разбор TrueType, подмножество глифов и встраивание шрифта в PDF.

Зачем всё это, если в PDF есть 14 встроенных шрифтов: ни в одном из них нет
кириллицы. Значит, шрифт надо встраивать. Целиком DejaVu Sans весит 750 КБ —
неприлично для одностраничного КП, поэтому берём только те глифы, которые
реально встретились в тексте: обычное предложение укладывается в 15–25 КБ.

Встраиваем как CIDFontType2 с кодировкой Identity-H: в контент-поток пишутся
не символы, а двухбайтовые номера глифов. Плюс кладём ToUnicode CMap, чтобы из
готового PDF можно было скопировать текст и получить нормальные буквы.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from .objects import Name, PDFFile, Ref, Stream

# Таблицы, без которых глифы не отрисуются. Всё остальное (name, post, OS/2,
# cmap) для Identity-H не нужно и только раздувает файл.
_KEEP_TABLES = ("head", "hhea", "maxp", "hmtx", "loca", "glyf", "cvt ", "fpgm", "prep")


class FontError(RuntimeError):
    """Шрифт не найден или не подходит для встраивания."""


def _checksum(data: bytes) -> int:
    padded = data + b"\x00" * (-len(data) % 4)
    total = 0
    for (word,) in struct.iter_unpack(">I", padded):
        total += word
    return total & 0xFFFFFFFF


class TrueTypeFont:
    """Минимальный разбор .ttf: ровно то, что нужно PDF-у."""

    def __init__(self, data: bytes, path: str | None = None) -> None:
        self.data = data
        self.path = path or "<память>"
        if len(data) < 12:
            raise FontError(f"{self.path}: файл слишком короткий для шрифта")
        tag = data[:4]
        if tag == b"ttcf":
            # Коллекция: берём первый шрифт внутри.
            offset = struct.unpack_from(">I", data, 12)[0]
        elif tag in (b"\x00\x01\x00\x00", b"true", b"OTTO"):
            offset = 0
        else:
            raise FontError(f"{self.path}: не похоже на TrueType/OpenType")

        num_tables = struct.unpack_from(">H", data, offset + 4)[0]
        self.tables: dict[str, tuple[int, int]] = {}
        for index in range(num_tables):
            base = offset + 12 + index * 16
            name, _sum, start, length = struct.unpack_from(">4sIII", data, base)
            self.tables[name.decode("latin-1")] = (start, length)

        if "glyf" not in self.tables or "loca" not in self.tables:
            raise FontError(
                f"{self.path}: шрифт с контурами CFF (обычно .otf) — нужен TrueType с таблицей glyf"
            )

        head = self._table("head")
        self.units_per_em = struct.unpack_from(">H", head, 18)[0] or 1000
        self.index_to_loc_format = struct.unpack_from(">h", head, 50)[0]
        x_min, y_min, x_max, y_max = struct.unpack_from(">hhhh", head, 36)
        self.bbox = (x_min, y_min, x_max, y_max)
        self.mac_style = struct.unpack_from(">H", head, 44)[0]

        maxp = self._table("maxp")
        self.num_glyphs = struct.unpack_from(">H", maxp, 4)[0]

        hhea = self._table("hhea")
        self.ascent = struct.unpack_from(">h", hhea, 4)[0]
        self.descent = struct.unpack_from(">h", hhea, 6)[0]
        self.num_h_metrics = struct.unpack_from(">H", hhea, 34)[0]

        self.italic_angle = 0.0
        if "post" in self.tables:
            post = self._table("post")
            whole, frac = struct.unpack_from(">hH", post, 4)
            self.italic_angle = whole + frac / 65536.0

        self.cap_height = int(self.ascent * 0.7)
        self.is_serif = False
        if "OS/2" in self.tables:
            os2 = self._table("OS/2")
            version = struct.unpack_from(">H", os2, 0)[0]
            if version >= 2 and len(os2) >= 90:
                cap = struct.unpack_from(">h", os2, 88)[0]
                if cap:
                    self.cap_height = cap
            family_class = struct.unpack_from(">h", os2, 30)[0] >> 8
            self.is_serif = family_class in (1, 2, 3, 4, 5, 7)

        self._loca = self._read_loca()
        self._hmtx = self._read_hmtx()
        self._cmap = self._read_cmap()

    def _table(self, name: str) -> bytes:
        if name not in self.tables:
            raise FontError(f"{self.path}: нет таблицы {name!r}")
        start, length = self.tables[name]
        return self.data[start : start + length]

    def _read_loca(self) -> list[int]:
        raw = self._table("loca")
        count = self.num_glyphs + 1
        if self.index_to_loc_format == 0:
            values = struct.unpack_from(f">{count}H", raw, 0)
            return [value * 2 for value in values]
        return list(struct.unpack_from(f">{count}I", raw, 0))

    def _read_hmtx(self) -> list[tuple[int, int]]:
        raw = self._table("hmtx")
        metrics: list[tuple[int, int]] = []
        for index in range(self.num_h_metrics):
            advance, bearing = struct.unpack_from(">Hh", raw, index * 4)
            metrics.append((advance, bearing))
        if not metrics:
            raise FontError(f"{self.path}: пустая таблица hmtx")
        # У моноширинных хвост глифов делит последнюю ширину.
        last = metrics[-1][0]
        tail_at = self.num_h_metrics * 4
        for index in range(self.num_glyphs - self.num_h_metrics):
            offset = tail_at + index * 2
            bearing = struct.unpack_from(">h", raw, offset)[0] if offset + 2 <= len(raw) else 0
            metrics.append((last, bearing))
        return metrics

    def _read_cmap(self) -> dict[int, int]:
        """Таблица «символ → глиф».

        Пустой словарь — законный результат: в подмножество cmap не попадает
        (Identity-H обходится без неё), а разбирать собранный субсет мы умеем.
        Отсутствие cmap в исходном шрифте ловится в `Document.face`.
        """
        if "cmap" not in self.tables:
            return {}
        raw = self._table("cmap")
        count = struct.unpack_from(">H", raw, 2)[0]
        best: tuple[int, int] | None = None  # (приоритет, смещение)
        for index in range(count):
            platform, encoding, offset = struct.unpack_from(">HHI", raw, 4 + index * 8)
            fmt = struct.unpack_from(">H", raw, offset)[0]
            if (platform, encoding) == (3, 10) and fmt == 12:
                priority = 4
            elif (platform, encoding) == (3, 1) and fmt == 4:
                priority = 3
            elif platform == 0 and fmt in (4, 12):
                priority = 2
            elif fmt in (4, 12, 6, 0):
                priority = 1
            else:
                continue
            if best is None or priority > best[0]:
                best = (priority, offset)
        if best is None:
            return {}

        offset = best[1]
        fmt = struct.unpack_from(">H", raw, offset)[0]
        if fmt == 4:
            return self._parse_cmap4(raw, offset)
        if fmt == 12:
            return self._parse_cmap12(raw, offset)
        if fmt == 6:
            first, entries = struct.unpack_from(">HH", raw, offset + 6)
            gids = struct.unpack_from(f">{entries}H", raw, offset + 10)
            return {first + i: gid for i, gid in enumerate(gids) if gid}
        table = raw[offset + 6 : offset + 6 + 256]
        return {code: gid for code, gid in enumerate(table) if gid}

    @staticmethod
    def _parse_cmap4(raw: bytes, offset: int) -> dict[int, int]:
        seg_x2 = struct.unpack_from(">H", raw, offset + 6)[0]
        segments = seg_x2 // 2
        ends = struct.unpack_from(f">{segments}H", raw, offset + 14)
        starts_at = offset + 16 + seg_x2
        starts = struct.unpack_from(f">{segments}H", raw, starts_at)
        deltas = struct.unpack_from(f">{segments}h", raw, starts_at + seg_x2)
        ranges_at = starts_at + seg_x2 * 2
        range_offsets = struct.unpack_from(f">{segments}H", raw, ranges_at)

        mapping: dict[int, int] = {}
        for seg in range(segments):
            start, end = starts[seg], ends[seg]
            if start > end or (end == 0xFFFF and start == 0xFFFF):
                continue
            for code in range(start, end + 1):
                if range_offsets[seg] == 0:
                    gid = (code + deltas[seg]) & 0xFFFF
                else:
                    at = ranges_at + seg * 2 + range_offsets[seg] + (code - start) * 2
                    if at + 2 > len(raw):
                        continue
                    gid = struct.unpack_from(">H", raw, at)[0]
                    if gid:
                        gid = (gid + deltas[seg]) & 0xFFFF
                if gid:
                    mapping[code] = gid
        return mapping

    @staticmethod
    def _parse_cmap12(raw: bytes, offset: int) -> dict[int, int]:
        groups = struct.unpack_from(">I", raw, offset + 12)[0]
        mapping: dict[int, int] = {}
        for index in range(groups):
            start, end, gid = struct.unpack_from(">III", raw, offset + 16 + index * 12)
            if end - start > 0x10000:  # защита от повреждённых таблиц
                end = start + 0x10000
            for step, code in enumerate(range(start, end + 1)):
                mapping[code] = gid + step
        return mapping

    # --- запросы, которыми пользуется вёрстка ---------------------------------

    def glyph_id(self, codepoint: int) -> int:
        """Номер глифа для символа; 0 (.notdef) — если символа в шрифте нет."""
        return self._cmap.get(codepoint, 0)

    def has(self, codepoint: int) -> bool:
        return codepoint in self._cmap

    @property
    def has_cmap(self) -> bool:
        return bool(self._cmap)

    def advance(self, gid: int) -> int:
        """Ширина глифа в единицах em."""
        if 0 <= gid < len(self._hmtx):
            return self._hmtx[gid][0]
        return self._hmtx[0][0]

    def scale(self, value: float) -> float:
        """Единицы шрифта → тысячные доли em (как принято в PDF)."""
        return value * 1000.0 / self.units_per_em

    def glyph_data(self, gid: int) -> bytes:
        start, _length = self.tables["glyf"]
        begin, end = self._loca[gid], self._loca[gid + 1]
        if end <= begin:
            return b""
        return self.data[start + begin : start + end]

    def components(self, gid: int) -> list[int]:
        """Составные глифы (Ё = Е + диерезис) тянут за собой части."""
        data = self.glyph_data(gid)
        if len(data) < 10:
            return []
        contours = struct.unpack_from(">h", data, 0)[0]
        if contours >= 0:
            return []
        parts: list[int] = []
        at = 10
        while at + 4 <= len(data):
            flags, index = struct.unpack_from(">HH", data, at)
            parts.append(index)
            at += 4
            at += 4 if flags & 0x0001 else 2  # ARG_1_AND_2_ARE_WORDS
            if flags & 0x0008:  # WE_HAVE_A_SCALE
                at += 2
            elif flags & 0x0040:  # X_AND_Y_SCALE
                at += 4
            elif flags & 0x0080:  # TWO_BY_TWO
                at += 8
            if not flags & 0x0020:  # MORE_COMPONENTS
                break
        return parts

    def subset(self, glyphs: set[int]) -> bytes:
        """Собрать новый .ttf, где есть контуры только нужных глифов.

        Нумерация глифов сохраняется: так контент-поток и таблица W остаются
        валидными без всякого перемапливания, а loca на 3000 записей — это
        12 КБ, из которых почти всё сжимается в ноль.
        """
        needed = {0} | {gid for gid in glyphs if 0 <= gid < self.num_glyphs}
        # Составные глифы: дотягиваем части, пока набор растёт.
        queue = list(needed)
        while queue:
            gid = queue.pop()
            for part in self.components(gid):
                if part not in needed and 0 <= part < self.num_glyphs:
                    needed.add(part)
                    queue.append(part)

        glyf = bytearray()
        loca: list[int] = []  # смещение начала каждого глифа в новой таблице glyf
        for gid in range(self.num_glyphs):
            loca.append(len(glyf))
            if gid in needed:
                chunk = self.glyph_data(gid)
                glyf += chunk
                glyf += b"\x00" * (-len(chunk) % 4)
        loca.append(len(glyf))

        long_loca = loca[-1] > 0x1FFFE or self.index_to_loc_format == 1
        if long_loca:
            loca_data = struct.pack(f">{len(loca)}I", *loca)
        else:
            loca_data = struct.pack(f">{len(loca)}H", *[value // 2 for value in loca])

        head = bytearray(self._table("head"))
        struct.pack_into(">I", head, 8, 0)  # checkSumAdjustment пересчитаем в конце
        struct.pack_into(">h", head, 50, 1 if long_loca else 0)

        tables: dict[str, bytes] = {}
        for name in _KEEP_TABLES:
            if name == "glyf":
                tables[name] = bytes(glyf)
            elif name == "loca":
                tables[name] = loca_data
            elif name == "head":
                tables[name] = bytes(head)
            elif name in self.tables:
                tables[name] = self._table(name)

        return self._assemble(tables)

    @staticmethod
    def _assemble(tables: dict[str, bytes]) -> bytes:
        names = sorted(tables)
        count = len(names)
        # searchRange и компания: степень двойки, ближайшая снизу к count.
        entry_selector = max(count.bit_length() - 1, 0)
        search_range = (1 << entry_selector) * 16

        header = struct.pack(
            ">IHHHH", 0x00010000, count, search_range, entry_selector, count * 16 - search_range
        )
        offset = 12 + count * 16
        directory = bytearray()
        body = bytearray()
        for name in names:
            data = tables[name]
            directory += struct.pack(
                ">4sIII", name.encode("latin-1"), _checksum(data), offset + len(body), len(data)
            )
            body += data + b"\x00" * (-len(data) % 4)

        font = bytearray(header + bytes(directory) + bytes(body))
        # checkSumAdjustment = 0xB1B0AFBA − контрольная сумма всего файла.
        head_at = None
        for index, name in enumerate(names):
            if name == "head":
                head_at = struct.unpack_from(">I", font, 12 + index * 16 + 8)[0]
                break
        if head_at is not None:
            adjustment = (0xB1B0AFBA - _checksum(bytes(font))) & 0xFFFFFFFF
            struct.pack_into(">I", font, head_at + 8, adjustment)
        return bytes(font)


@dataclass(slots=True)
class FontFace:
    """Один начертанный шрифт в документе: файл + накопленные глифы."""

    key: str
    ttf: TrueTypeFont
    ps_name: str
    used: set[int] = field(default_factory=set)
    _cache: dict[int, tuple[int, float]] = field(default_factory=dict, repr=False)

    def glyph(self, char: str) -> tuple[int, float]:
        """(номер глифа, ширина в долях em) с запоминанием — вёрстка зовёт часто."""
        code = ord(char)
        hit = self._cache.get(code)
        if hit is None:
            gid = self.ttf.glyph_id(code)
            hit = (gid, self.ttf.scale(self.ttf.advance(gid)) / 1000.0)
            self._cache[code] = hit
        return hit

    def measure(self, text: str, size: float) -> float:
        total = 0.0
        for char in text:
            gid, width = self.glyph(char)
            self.used.add(gid)
            total += width
        return total * size

    def encode(self, text: str) -> bytes:
        """Строка → шестнадцатеричные номера глифов для оператора Tj."""
        out = bytearray()
        for char in text:
            gid, _ = self.glyph(char)
            self.used.add(gid)
            out += struct.pack(">H", gid)
        return bytes(out)

    def missing(self, text: str) -> set[str]:
        """Символы, которых в шрифте нет, — рисовать их бессмысленно."""
        return {char for char in text if not self.ttf.has(ord(char)) and char not in "\n\t"}

    def write(self, pdf: PDFFile) -> Ref:
        """Положить шрифт в документ и вернуть ссылку на объект /Font."""
        ttf = self.ttf
        subset = ttf.subset(self.used)
        file_ref = pdf.add(
            Stream(subset, {"Length1": len(subset)})
        )

        flags = 4  # Symbolic: у нас произвольная кодировка глифов
        if ttf.is_serif:
            flags |= 2
        if ttf.italic_angle:
            flags |= 64
        if ttf.mac_style & 0x0001:
            flags |= 1 << 18  # ForceBold

        scale = ttf.scale
        descriptor = pdf.add(
            {
                "Type": Name("FontDescriptor"),
                "FontName": Name(self.ps_name),
                "Flags": flags,
                "FontBBox": [round(scale(value)) for value in ttf.bbox],
                "ItalicAngle": round(ttf.italic_angle, 2),
                "Ascent": round(scale(ttf.ascent)),
                "Descent": round(scale(ttf.descent)),
                "CapHeight": round(scale(ttf.cap_height)),
                "StemV": 120 if ttf.mac_style & 0x0001 else 80,
                "FontFile2": file_ref,
            }
        )

        descendant = pdf.add(
            {
                "Type": Name("Font"),
                "Subtype": Name("CIDFontType2"),
                "BaseFont": Name(self.ps_name),
                "CIDSystemInfo": {
                    "Registry": b"Adobe",
                    "Ordering": b"Identity",
                    "Supplement": 0,
                },
                "FontDescriptor": descriptor,
                "DW": round(scale(ttf.advance(0))) or 1000,
                "W": self._widths(),
                "CIDToGIDMap": Name("Identity"),
            }
        )

        return pdf.add(
            {
                "Type": Name("Font"),
                "Subtype": Name("Type0"),
                "BaseFont": Name(self.ps_name),
                "Encoding": Name("Identity-H"),
                "DescendantFonts": [descendant],
                "ToUnicode": pdf.add(Stream(self._to_unicode())),
            }
        )

    def _widths(self) -> list[object]:
        """Таблица W: подряд идущие глифы группируем в один диапазон."""
        scale = self.ttf.scale
        widths = {gid: round(scale(self.ttf.advance(gid))) for gid in sorted(self.used)}
        result: list[object] = []
        run_start: int | None = None
        run: list[int] = []
        for gid in sorted(widths):
            if run_start is not None and gid == run_start + len(run):
                run.append(widths[gid])
                continue
            if run_start is not None:
                result += [run_start, run]
            run_start, run = gid, [widths[gid]]
        if run_start is not None:
            result += [run_start, run]
        return result

    def _to_unicode(self) -> bytes:
        """CMap «глиф → символ», чтобы из PDF копировался текст, а не кракозябры."""
        pairs: list[tuple[int, int]] = []
        seen: set[int] = set()
        for code, gid in self.ttf._cmap.items():
            if gid in self.used and gid not in seen:
                seen.add(gid)
                pairs.append((gid, code))
        pairs.sort()

        head = (
            "/CIDInit /ProcSet findresource begin\n"
            "12 dict begin\nbegincmap\n"
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
            "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        )
        body: list[str] = []
        for start in range(0, len(pairs), 100):
            chunk = pairs[start : start + 100]
            body.append(f"{len(chunk)} beginbfchar")
            for gid, code in chunk:
                target = code if code <= 0xFFFF else 0xFFFD
                body.append(f"<{gid:04X}> <{target:04X}>")
            body.append("endbfchar")
        tail = "endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
        return (head + "\n".join(body) + "\n" + tail).encode("ascii")


# --- поиск шрифтов в системе ---------------------------------------------------

# Семейства, в которых точно есть кириллица, в порядке предпочтения.
_FAMILIES: tuple[tuple[str, dict[str, tuple[str, ...]]], ...] = (
    (
        "DejaVu Sans",
        {
            "regular": ("DejaVuSans.ttf",),
            "bold": ("DejaVuSans-Bold.ttf",),
            "italic": ("DejaVuSans-Oblique.ttf",),
            "bolditalic": ("DejaVuSans-BoldOblique.ttf",),
        },
    ),
    (
        "Liberation Sans",
        {
            "regular": ("LiberationSans-Regular.ttf",),
            "bold": ("LiberationSans-Bold.ttf",),
            "italic": ("LiberationSans-Italic.ttf",),
            "bolditalic": ("LiberationSans-BoldItalic.ttf",),
        },
    ),
    (
        "Noto Sans",
        {
            "regular": ("NotoSans-Regular.ttf",),
            "bold": ("NotoSans-Bold.ttf",),
            "italic": ("NotoSans-Italic.ttf",),
            "bolditalic": ("NotoSans-BoldItalic.ttf",),
        },
    ),
    (
        "Free Sans",
        {
            "regular": ("FreeSans.ttf",),
            "bold": ("FreeSansBold.ttf",),
            "italic": ("FreeSansOblique.ttf",),
            "bolditalic": ("FreeSansBoldOblique.ttf",),
        },
    ),
    (
        "Arial",
        {
            "regular": ("arial.ttf", "Arial.ttf", "Arial Unicode.ttf"),
            "bold": ("arialbd.ttf", "Arial Bold.ttf"),
            "italic": ("ariali.ttf", "Arial Italic.ttf"),
            "bolditalic": ("arialbi.ttf", "Arial Bold Italic.ttf"),
        },
    ),
)

_SEARCH_DIRS = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "~/.fonts",
    "~/.local/share/fonts",
    "/Library/Fonts",
    "/System/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    "~/Library/Fonts",
    "C:/Windows/Fonts",
)


def _index_system_fonts() -> dict[str, Path]:
    """Один обход каталогов: имя файла в нижнем регистре → путь."""
    found: dict[str, Path] = {}
    for raw in _SEARCH_DIRS:
        root = Path(raw).expanduser()
        if not root.is_dir():
            continue
        try:
            for path in root.rglob("*.ttf"):
                found.setdefault(path.name.lower(), path)
        except OSError:
            continue
    return found


@dataclass(frozen=True, slots=True)
class FontFamily:
    """Четыре начертания одного семейства."""

    name: str
    regular: Path
    bold: Path | None = None
    italic: Path | None = None
    bolditalic: Path | None = None

    def path_for(self, bold: bool, italic: bool) -> Path:
        if bold and italic and self.bolditalic:
            return self.bolditalic
        if bold and self.bold:
            return self.bold
        if italic and self.italic:
            return self.italic
        return self.regular


def find_family(preferred: str | Path | None = None) -> FontFamily:
    """Найти семейство с кириллицей.

    `preferred` — путь к .ttf (тогда начертания ищутся рядом по соседним файлам
    того же семейства) или название семейства из списка известных.
    """
    if preferred:
        as_path = Path(preferred).expanduser()
        if as_path.suffix.lower() == ".ttf" or as_path.is_file():
            if not as_path.is_file():
                raise FontError(f"шрифт не найден: {as_path}")
            return _family_from_path(as_path)

    index = _index_system_fonts()
    wanted = str(preferred).strip().lower() if preferred else None

    for family_name, styles in _FAMILIES:
        if wanted and wanted not in family_name.lower():
            continue
        regular = _first_existing(styles["regular"], index)
        if regular is None:
            continue
        return FontFamily(
            name=family_name,
            regular=regular,
            bold=_first_existing(styles["bold"], index),
            italic=_first_existing(styles["italic"], index),
            bolditalic=_first_existing(styles["bolditalic"], index),
        )

    if wanted:
        raise FontError(
            f"шрифт {preferred!r} не найден. Укажите путь к .ttf через --font "
            "или поставьте пакет со шрифтом."
        )
    raise FontError(
        "в системе не нашлось шрифта с кириллицей. Поставьте любой из них "
        "(Debian/Ubuntu: apt install fonts-dejavu-core, "
        "Fedora: dnf install dejavu-sans-fonts) или укажите свой: --font /путь/к/шрифту.ttf"
    )


def _first_existing(names: tuple[str, ...], index: dict[str, Path]) -> Path | None:
    for name in names:
        hit = index.get(name.lower())
        if hit is not None:
            return hit
    return None


def _family_from_path(path: Path) -> FontFamily:
    """По файлу обычного начертания угадать соседние Bold/Italic."""
    stem, folder = path.stem, path.parent

    def sibling(*suffixes: str) -> Path | None:
        for suffix in suffixes:
            for candidate in (
                folder / f"{stem}{suffix}.ttf",
                folder / f"{stem}-{suffix}.ttf",
            ):
                if candidate.is_file():
                    return candidate
            # LiberationSans-Regular → LiberationSans-Bold
            if "-" in stem:
                base = stem.rsplit("-", 1)[0]
                candidate = folder / f"{base}-{suffix}.ttf"
                if candidate.is_file():
                    return candidate
        return None

    return FontFamily(
        name=stem,
        regular=path,
        bold=sibling("Bold", "bd"),
        italic=sibling("Italic", "Oblique"),
        bolditalic=sibling("BoldItalic", "BoldOblique"),
    )


_LOADED: dict[Path, TrueTypeFont] = {}


def load(path: Path) -> TrueTypeFont:
    """Прочитать .ttf с кэшем: одно семейство переиспользуется между КП."""
    resolved = Path(path).expanduser().resolve()
    cached = _LOADED.get(resolved)
    if cached is None:
        try:
            data = resolved.read_bytes()
        except OSError as error:
            raise FontError(f"не читается шрифт {resolved}: {error}") from error
        cached = TrueTypeFont(data, str(resolved))
        _LOADED[resolved] = cached
    return cached
