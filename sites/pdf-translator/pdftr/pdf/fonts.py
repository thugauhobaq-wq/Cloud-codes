"""Шрифты, какими они лежат внутри чужого PDF.

Байты в контент-потоке — это не текст. `<0037004b0048>` может означать «The», а
может «Ν∆λ»: смысл задаёт шрифт. Чтобы перевести документ, надо для каждого
шрифта ответить на два вопроса:

* какой символ Юникода стоит за кодом — иначе переводить нечего;
* какой ширины этот символ — иначе неизвестно, где кончается строка и где
  начинается соседняя колонка.

Источников ответа несколько, и они перебираются по убыванию надёжности:
ToUnicode CMap (её кладут сами генераторы PDF), затем /Encoding с /Differences,
затем встроенная в шрифт таблица cmap, и в самом конце — latin-1 наугад.
Если внятного ответа нет, шрифт помечается ненадёжным: такой текст мы не
переводим и не трогаем, чтобы не испортить страницу мусором.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .embed import TrueTypeFont
from .objects import Name
from .reader import Document, Lexer, StreamObject, _as_number

# StandardEncoding: единственная из базовых кодировок, которой нет среди кодеков
# Python. Остальные берём готовыми — cp1252 и mac_roman совпадают с ними.
_STANDARD_NAMES = {
    32: "space", 33: "exclam", 34: "quotedbl", 35: "numbersign", 36: "dollar",
    37: "percent", 38: "ampersand", 39: "quoteright", 40: "parenleft",
    41: "parenright", 42: "asterisk", 43: "plus", 44: "comma", 45: "hyphen",
    46: "period", 47: "slash", 48: "zero", 49: "one", 50: "two", 51: "three",
    52: "four", 53: "five", 54: "six", 55: "seven", 56: "eight", 57: "nine",
    58: "colon", 59: "semicolon", 60: "less", 61: "equal", 62: "greater",
    63: "question", 64: "at", 91: "bracketleft", 92: "backslash",
    93: "bracketright", 94: "asciicircum", 95: "underscore", 96: "quoteleft",
    123: "braceleft", 124: "bar", 125: "braceright", 126: "asciitilde",
    161: "exclamdown", 162: "cent", 163: "sterling", 164: "fraction",
    165: "yen", 166: "florin", 167: "section", 168: "currency",
    169: "quotesingle", 170: "quotedblleft", 171: "guillemotleft",
    172: "guilsinglleft", 173: "guilsinglright", 174: "fi", 175: "fl",
    177: "endash", 178: "dagger", 179: "daggerdbl", 180: "periodcentered",
    182: "paragraph", 183: "bullet", 184: "quotesinglbase", 185: "quotedblbase",
    186: "quotedblright", 187: "guillemotright", 188: "ellipsis",
    189: "perthousand", 191: "questiondown", 193: "grave", 194: "acute",
    195: "circumflex", 196: "tilde", 197: "macron", 198: "breve",
    199: "dotaccent", 200: "dieresis", 202: "ring", 203: "cedilla",
    205: "hungarumlaut", 206: "ogonek", 207: "caron", 208: "emdash",
    225: "AE", 227: "ordfeminine", 232: "Lslash", 233: "Oslash", 234: "OE",
    235: "ordmasculine", 241: "ae", 245: "dotlessi", 248: "lslash",
    249: "oslash", 250: "oe", 251: "germandbls",
}
for _code in range(65, 91):
    _STANDARD_NAMES[_code] = chr(_code)
for _code in range(97, 123):
    _STANDARD_NAMES[_code] = chr(_code)

# Имена глифов, которых не вывести из кодировок: лигатуры из TeX, типографика.
_EXTRA_NAMES = {
    "ff": "ff", "fi": "fi", "fl": "fl", "ffi": "ffi", "ffl": "ffl",
    "quoteright": "’", "quoteleft": "‘", "quotedblleft": "“",
    "quotedblright": "”", "quotesinglbase": "‚",
    "quotedblbase": "„", "endash": "–", "emdash": "—",
    "bullet": "•", "ellipsis": "…", "dagger": "†",
    "daggerdbl": "‡", "perthousand": "‰", "fraction": "⁄",
    "guilsinglleft": "‹", "guilsinglright": "›", "minus": "−",
    "florin": "ƒ", "Lslash": "Ł", "lslash": "ł",
    "dotlessi": "ı", "trademark": "™", "notequal": "≠",
    "lessequal": "≤", "greaterequal": "≥", "partialdiff": "∂",
    "summation": "∑", "product": "∏", "radical": "√",
    "infinity": "∞", "integral": "∫", "approxequal": "≈",
    "Delta": "∆", "Omega": "Ω", "pi": "π", "mu": "μ",
    "nbspace": " ", "space": " ", "sfthyphen": "-", "hyphen": "-",
}


# Латиница с диакритикой: имена из Adobe Glyph List. Вывести их из Юникода
# нельзя — они историчны, поэтому таблица явная. Прописные строятся из строчных
# по правилу «имя с большой буквы», кроме нескольких исключений ниже.
_ACCENTED = {
    "agrave": "à", "aacute": "á", "acircumflex": "â", "atilde": "ã",
    "adieresis": "ä", "aring": "å", "ccedilla": "ç", "egrave": "è",
    "eacute": "é", "ecircumflex": "ê", "edieresis": "ë", "igrave": "ì",
    "iacute": "í", "icircumflex": "î", "idieresis": "ï", "eth": "ð",
    "ntilde": "ñ", "ograve": "ò", "oacute": "ó", "ocircumflex": "ô",
    "otilde": "õ", "odieresis": "ö", "oslash": "ø", "ugrave": "ù",
    "uacute": "ú", "ucircumflex": "û", "udieresis": "ü", "yacute": "ý",
    "thorn": "þ", "scaron": "š", "zcaron": "ž", "ccaron": "č",
    "ecaron": "ě", "rcaron": "ř", "aogonek": "ą", "eogonek": "ę",
    "lacute": "ĺ", "nacute": "ń", "sacute": "ś", "zacute": "ź",
    "zdotaccent": "ż", "amacron": "ā", "emacron": "ē", "imacron": "ī",
    "omacron": "ō", "umacron": "ū",
}
_UPPER_EXCEPTIONS = {"ae": "AE", "oe": "OE", "germandbls": "SS", "dotlessi": "I"}

# Знаки препинания и символы: имя одинаково во всех базовых кодировках.
_PUNCTUATION = {
    "space": " ", "exclam": "!", "quotedbl": '"', "numbersign": "#",
    "dollar": "$", "percent": "%", "ampersand": "&", "quotesingle": "'",
    "parenleft": "(", "parenright": ")", "asterisk": "*", "plus": "+",
    "comma": ",", "hyphen": "-", "period": ".", "slash": "/",
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "colon": ":", "semicolon": ";", "less": "<", "equal": "=",
    "greater": ">", "question": "?", "at": "@", "bracketleft": "[",
    "backslash": "\\", "bracketright": "]", "asciicircum": "^",
    "underscore": "_", "grave": "`", "braceleft": "{", "bar": "|",
    "braceright": "}", "asciitilde": "~", "exclamdown": "¡", "cent": "¢",
    "sterling": "£", "currency": "¤", "yen": "¥", "brokenbar": "¦",
    "section": "§", "dieresis": "¨", "copyright": "©", "ordfeminine": "ª",
    "guillemotleft": "«", "logicalnot": "¬", "registered": "®",
    "macron": "¯", "degree": "°", "plusminus": "±", "acute": "´",
    "paragraph": "¶", "periodcentered": "·", "cedilla": "¸",
    "ordmasculine": "º", "guillemotright": "»", "questiondown": "¿",
    "multiply": "×", "divide": "÷", "onehalf": "½", "onequarter": "¼",
    "threequarters": "¾", "onesuperior": "¹", "twosuperior": "²",
    "threesuperior": "³", "ae": "æ", "oe": "œ", "germandbls": "ß",
    "circumflex": "ˆ", "tilde": "˜", "breve": "˘", "dotaccent": "˙",
    "ring": "˚", "ogonek": "˛", "caron": "ˇ", "hungarumlaut": "˝",
}

GLYPH_NAMES: dict[str, str] = dict(_PUNCTUATION)
for _name, _char in _ACCENTED.items():
    GLYPH_NAMES[_name] = _char
    GLYPH_NAMES[_name[:1].upper() + _name[1:]] = _char.upper()
for _name, _char in _UPPER_EXCEPTIONS.items():
    GLYPH_NAMES[_name[:1].upper() + _name[1:]] = _char
for _code in range(65, 91):  # A–Z и a–z называются сами собой
    GLYPH_NAMES[chr(_code)] = chr(_code)
    GLYPH_NAMES[chr(_code + 32)] = chr(_code + 32)
GLYPH_NAMES.update(_EXTRA_NAMES)

_UNI_NAME = re.compile(r"^uni([0-9A-Fa-f]{4,6})$")
_U_NAME = re.compile(r"^u([0-9A-Fa-f]{4,6})$")


def glyph_to_char(name: str) -> str:
    """Имя глифа → символ Юникода. Пустая строка, если имя ни о чём не говорит."""
    hit = GLYPH_NAMES.get(name)
    if hit:
        return hit
    match = _UNI_NAME.match(name) or _U_NAME.match(name)
    if match:
        try:
            return chr(int(match.group(1), 16))
        except ValueError:
            return ""
    # `A.sc`, `one.oldstyle` — вариант глифа, базовое имя стоит до точки.
    if "." in name:
        return glyph_to_char(name.split(".", 1)[0])
    return ""


def parse_cmap(data: bytes) -> tuple[dict[int, str], list[tuple[int, int, int]]]:
    """Разбор CMap: коды → символы и диапазоны кодов с длиной в байтах.

    Понимает и ToUnicode (bfchar/bfrange), и встроенные CMap кодировок
    (cidchar/cidrange) — устройство у них одинаковое.
    """
    mapping: dict[int, str] = {}
    spaces: list[tuple[int, int, int]] = []
    lexer = Lexer(data, 0)
    stack: list[object] = []
    mode: str | None = None

    def as_int(value: object) -> int | None:
        if isinstance(value, bytes):
            return int.from_bytes(value, "big") if value else None
        if isinstance(value, int):
            return value
        return None

    def as_text(value: object) -> str:
        if isinstance(value, bytes):
            if len(value) % 2 == 0 and value:
                try:
                    text = value.decode("utf-16-be")
                except UnicodeDecodeError:
                    text = value.decode("latin-1")
            else:
                text = value.decode("latin-1")
            return text.replace("\x00", "")
        if isinstance(value, int):
            return chr(value) if 0 < value < 0x110000 else ""
        return ""

    while True:
        token = lexer.read_token()
        if token is None:
            break
        if token == b"(":
            stack.append(lexer.read_literal_string())
            continue
        if token == b"<":
            stack.append(lexer.read_hex_string())
            continue
        if token == b"/":
            stack.append(lexer.read_name())
            continue
        if token == b"[":
            items: list[object] = []
            while True:
                inner = lexer.read_token()
                if inner is None or inner == b"]":
                    break
                if inner == b"<":
                    items.append(lexer.read_hex_string())
                elif inner == b"(":
                    items.append(lexer.read_literal_string())
                elif inner == b"/":
                    items.append(lexer.read_name())
                else:
                    items.append(_as_number(inner))
            stack.append(items)
            continue
        word = token.decode("latin-1", "replace")
        if word.startswith("begin") and word.endswith(("char", "range", "codespacerange")):
            mode = word[5:]
            stack.clear()
            continue
        if word.startswith("end"):
            group = word[3:]
            if group == "codespacerange":
                for index in range(0, len(stack) - 1, 2):
                    low, high = stack[index], stack[index + 1]
                    if isinstance(low, bytes) and isinstance(high, bytes):
                        spaces.append(
                            (
                                int.from_bytes(low, "big"),
                                int.from_bytes(high, "big"),
                                len(low) or 1,
                            )
                        )
            elif group in ("bfchar", "cidchar"):
                for index in range(0, len(stack) - 1, 2):
                    code = as_int(stack[index])
                    if code is None:
                        continue
                    mapping[code] = as_text(stack[index + 1])
            elif group in ("bfrange", "cidrange"):
                for index in range(0, len(stack) - 2, 3):
                    low = as_int(stack[index])
                    high = as_int(stack[index + 1])
                    target = stack[index + 2]
                    if low is None or high is None or high < low:
                        continue
                    high = min(high, low + 65535)  # защита от испорченных диапазонов
                    if isinstance(target, list):
                        for shift, item in enumerate(target):
                            mapping[low + shift] = as_text(item)
                    elif isinstance(target, bytes):
                        base = as_text(target)
                        if not base:
                            continue
                        start = ord(base[-1])
                        prefix = base[:-1]
                        for shift in range(high - low + 1):
                            point = start + shift
                            if point > 0x10FFFF:
                                break
                            mapping[low + shift] = prefix + chr(point)
                    elif isinstance(target, int):
                        for shift in range(high - low + 1):
                            mapping[low + shift] = chr(target + shift)
            mode = None
            stack.clear()
            continue
        number = _as_number(token)
        if number is not None:
            stack.append(number)
        elif mode is None:
            stack.clear()
    return mapping, spaces


@dataclass(slots=True)
class Font:
    """Шрифт страницы: как читать его байты и сколько места занимают символы."""

    key: str
    subtype: str = "Type1"
    base_font: str = ""
    two_byte: bool = False
    codespaces: list[tuple[int, int, int]] = field(default_factory=list)
    to_unicode: dict[int, str] = field(default_factory=dict)
    encoding: dict[int, str] = field(default_factory=dict)
    widths: dict[int, float] = field(default_factory=dict)
    default_width: float = 500.0
    bold: bool = False
    italic: bool = False
    serif: bool = False
    ascent: float = 750.0
    descent: float = -250.0
    reliable: bool = True

    def codes(self, raw: bytes) -> list[int]:
        """Байты строки → последовательность кодов символов."""
        if not self.codespaces:
            if self.two_byte:
                return [
                    int.from_bytes(raw[i : i + 2], "big") for i in range(0, len(raw) - 1, 2)
                ]
            return list(raw)
        out: list[int] = []
        index = 0
        size = len(raw)
        while index < size:
            for length in (1, 2, 3, 4):
                if index + length > size:
                    continue
                value = int.from_bytes(raw[index : index + length], "big")
                if any(
                    low <= value <= high and width == length
                    for low, high, width in self.codespaces
                ):
                    out.append(value)
                    index += length
                    break
            else:
                length = 2 if self.two_byte and index + 1 < size else 1
                out.append(int.from_bytes(raw[index : index + length], "big"))
                index += length
        return out

    def char(self, code: int) -> str:
        text = self.to_unicode.get(code)
        if text is not None:
            return text
        text = self.encoding.get(code)
        if text is not None:
            return text
        if self.two_byte:
            return ""
        if 32 <= code < 127:
            return chr(code)
        return bytes([code & 0xFF]).decode("cp1252", "ignore")

    def width(self, code: int) -> float:
        """Ширина кода в тысячных долях кегля."""
        hit = self.widths.get(code)
        return self.default_width if hit is None else hit

    def decode(self, raw: bytes) -> list[tuple[int, str, float]]:
        """Строка → список (код, символ, ширина)."""
        return [(code, self.char(code), self.width(code)) for code in self.codes(raw)]


# Метрики четырнадцати базовых шрифтов нужны только приблизительно: по ним мы
# считаем ширину блока, а не рисуем. Моноширинный и пропорциональный разбег
# куда важнее, чем точность до единиц.
_BUILTIN_NARROW = set(" iljt.,;:'`!|[]()froman")


def _builtin_widths(base: str) -> tuple[dict[int, float], float]:
    name = base.lower()
    if "courier" in name or "mono" in name:
        return {}, 600.0
    widths: dict[int, float] = {}
    bold = "bold" in name
    for code in range(32, 256):
        char = chr(code)
        if char == " ":
            value = 278.0
        elif char.isdigit():
            value = 556.0
        elif char.isupper():
            value = 722.0 if char in "MW" else 667.0
        elif char in _BUILTIN_NARROW:
            value = 278.0 if char in " iljt.,;:'`!|" else 500.0
        elif char.islower():
            value = 889.0 if char in "mw" else 556.0
        else:
            value = 556.0
        widths[code] = value + (28.0 if bold else 0.0)
    return widths, 556.0


def _flags_of(name: str, flags: int) -> tuple[bool, bool, bool]:
    lowered = name.lower()
    bold = "bold" in lowered or "black" in lowered or "heavy" in lowered
    italic = "italic" in lowered or "oblique" in lowered
    serif = "times" in lowered or "serif" in lowered or "roman" in lowered
    if flags:
        serif = serif or bool(flags & 2)
        italic = italic or bool(flags & 64)
        bold = bold or bool(flags & (1 << 18))
    return bold, italic, serif


def _base_encoding(doc: Document, source: object, symbolic: bool) -> dict[int, str]:
    """Кодировка простого шрифта: базовая таблица плюс /Differences."""
    table: dict[int, str] = {}
    base_name = ""
    differences: list | None = None
    resolved = doc.resolve(source)
    if isinstance(resolved, Name):
        base_name = resolved.value
    elif isinstance(resolved, dict):
        inner = doc.resolve(resolved.get("BaseEncoding"))
        if isinstance(inner, Name):
            base_name = inner.value
        raw_diff = doc.resolve(resolved.get("Differences"))
        if isinstance(raw_diff, list):
            differences = raw_diff

    if base_name == "WinAnsiEncoding" or (not base_name and not symbolic):
        for code in range(32, 256):
            char = bytes([code]).decode("cp1252", "ignore")
            if char:
                table[code] = char
        table[0xA0] = " "
        table[0xAD] = "-"
    elif base_name == "MacRomanEncoding":
        for code in range(32, 256):
            char = bytes([code]).decode("mac_roman", "ignore")
            if char:
                table[code] = char
    elif base_name in ("StandardEncoding", "MacExpertEncoding") or base_name:
        for code, glyph in _STANDARD_NAMES.items():
            char = glyph_to_char(glyph)
            if char:
                table[code] = char

    if differences:
        code = 0
        for item in differences:
            item = doc.resolve(item)
            if isinstance(item, (int, float)):
                code = int(item)
            elif isinstance(item, Name):
                char = glyph_to_char(item.value)
                if char:
                    table[code] = char
                else:
                    table.pop(code, None)
                code += 1
    return table


def _embedded_cmap(doc: Document, descriptor: object) -> dict[int, str]:
    """Кодировка из самого файла шрифта — последняя надежда для symbolic-шрифтов."""
    if not isinstance(descriptor, dict):
        return {}
    stream = doc.resolve(descriptor.get("FontFile2"))
    if not isinstance(stream, StreamObject):
        return {}
    try:
        ttf = TrueTypeFont(stream.decoded())
        table = ttf._read_cmap()
    except Exception:
        return {}
    out: dict[int, str] = {}
    for point, _gid in table.items():
        # Symbolic-шрифты держат глифы в приватной области 0xF000–0xF0FF.
        if 0xF000 <= point <= 0xF0FF:
            out.setdefault(point & 0xFF, chr(point & 0xFF))
        elif point < 0x110000:
            out.setdefault(point, chr(point))
    return out


def _cid_widths(doc: Document, raw: object) -> dict[int, float]:
    """Массив /W: `[ 1 [250 333] 5 10 500 ]` — по одному и диапазонами."""
    widths: dict[int, float] = {}
    items = doc.resolve(raw)
    if not isinstance(items, list):
        return widths
    index = 0
    while index < len(items):
        first = doc.resolve(items[index])
        if not isinstance(first, (int, float)):
            index += 1
            continue
        if index + 1 >= len(items):
            break
        second = doc.resolve(items[index + 1])
        if isinstance(second, list):
            for shift, value in enumerate(second):
                value = doc.resolve(value)
                if isinstance(value, (int, float)):
                    widths[int(first) + shift] = float(value)
            index += 2
        elif index + 2 < len(items):
            third = doc.resolve(items[index + 2])
            start, stop = int(first), int(second)
            if isinstance(third, (int, float)) and 0 <= stop - start <= 65535:
                for code in range(start, stop + 1):
                    widths[code] = float(third)
            index += 3
        else:
            break
    return widths


def load_font(doc: Document, key: str, source: object) -> Font:
    """Собрать описание шрифта из его словаря в ресурсах страницы."""
    info = doc.resolve(source)
    if isinstance(info, StreamObject):
        info = info.info
    if not isinstance(info, dict):
        return Font(key=key, reliable=False)

    subtype = doc.resolve(info.get("Subtype"))
    subtype = subtype.value if isinstance(subtype, Name) else "Type1"
    base = doc.resolve(info.get("BaseFont"))
    base_font = base.value if isinstance(base, Name) else ""
    if len(base_font) > 7 and base_font[6] == "+":
        base_font = base_font[7:]  # префикс подмножества: ABCDEF+Times

    font = Font(key=key, subtype=subtype, base_font=base_font)

    raw_tounicode = doc.resolve(info.get("ToUnicode"))
    if isinstance(raw_tounicode, StreamObject):
        try:
            mapping, spaces = parse_cmap(raw_tounicode.decoded())
        except (ValueError, IndexError):
            mapping, spaces = {}, []
        font.to_unicode = {code: text for code, text in mapping.items() if text}
        if spaces:
            font.codespaces = spaces

    if subtype == "Type0":
        font.two_byte = True
        encoding = doc.resolve(info.get("Encoding"))
        if isinstance(encoding, StreamObject):
            _mapping, spaces = parse_cmap(encoding.decoded())
            if spaces:
                font.codespaces = font.codespaces or spaces
        descendants = doc.resolve(info.get("DescendantFonts"))
        child = None
        if isinstance(descendants, list) and descendants:
            child = doc.resolve(descendants[0])
        if isinstance(child, dict):
            default = doc.resolve(child.get("DW"))
            font.default_width = float(default) if isinstance(default, (int, float)) else 1000.0
            font.widths = _cid_widths(doc, child.get("W"))
            descriptor = doc.resolve(child.get("FontDescriptor"))
        else:
            font.default_width = 1000.0
            descriptor = None
        font.reliable = bool(font.to_unicode)
    else:
        descriptor = doc.resolve(info.get("FontDescriptor"))
        flags = doc.resolve(descriptor.get("Flags")) if isinstance(descriptor, dict) else 0
        flags = int(flags) if isinstance(flags, (int, float)) else 0
        symbolic = bool(flags & 4) and not flags & 32
        font.encoding = _base_encoding(doc, info.get("Encoding"), symbolic)
        if not font.encoding and not font.to_unicode:
            font.encoding = _embedded_cmap(doc, descriptor)

        first = doc.resolve(info.get("FirstChar"))
        raw_widths = doc.resolve(info.get("Widths"))
        if isinstance(raw_widths, list) and isinstance(first, (int, float)):
            for shift, value in enumerate(raw_widths):
                value = doc.resolve(value)
                if isinstance(value, (int, float)):
                    font.widths[int(first) + shift] = float(value)
        missing = None
        if isinstance(descriptor, dict):
            missing = doc.resolve(descriptor.get("MissingWidth"))
        if isinstance(missing, (int, float)):
            font.default_width = float(missing)
        if not font.widths:
            font.widths, font.default_width = _builtin_widths(base_font)
        # Type3 живёт в своей системе координат — трогать такой текст опасно.
        font.reliable = subtype != "Type3" and bool(
            font.to_unicode or font.encoding or not symbolic
        )

    if isinstance(descriptor, dict):
        flags = doc.resolve(descriptor.get("Flags"))
        flags = int(flags) if isinstance(flags, (int, float)) else 0
    else:
        flags = 0
    font.bold, font.italic, font.serif = _flags_of(base_font, flags)
    if isinstance(descriptor, dict):
        for key, attribute, fallback in (
            ("Ascent", "ascent", 750.0),
            ("Descent", "descent", -250.0),
        ):
            value = doc.resolve(descriptor.get(key))
            if isinstance(value, (int, float)) and value:
                setattr(font, attribute, float(value))
            else:
                setattr(font, attribute, fallback)
        weight = doc.resolve(descriptor.get("StemV"))
        if isinstance(weight, (int, float)) and weight >= 120:
            font.bold = True
    return font


def page_fonts(doc: Document, resources: dict) -> dict[str, Font]:
    """Все шрифты, объявленные в ресурсах страницы, по их именам (`/F1`)."""
    table = doc.resolve(resources.get("Font"))
    out: dict[str, Font] = {}
    if not isinstance(table, dict):
        return out
    for key, value in table.items():
        name = key.value if isinstance(key, Name) else str(key)
        try:
            out[name] = load_font(doc, name, value)
        except (ValueError, TypeError, KeyError, IndexError):
            out[name] = Font(key=name, reliable=False)
    return out
