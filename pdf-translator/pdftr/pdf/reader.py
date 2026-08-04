"""Чтение PDF: синтаксис, таблица объектов, дерево страниц.

Файл PDF читается не с начала, а с конца: в последних байтах лежит `startxref`
со смещением таблицы перекрёстных ссылок, а уже она говорит, где какой объект.
Таблиц может быть несколько (документ правился и дописывался), они связаны
полем /Prev в обратном порядке — побеждает та, что ближе к концу файла.

На практике таблицы часто врут: файл склеили из двух, обрезали, дописали
руками. Поэтому при любой заминке мы просто прочёсываем файл на «N G obj» и
строим таблицу заново — так открываются даже документы, которые не открывает
половина просмотрщиков.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .filters import IMAGE_FILTERS, FilterError, decode
from .objects import Name, Ref

WHITESPACE = b"\x00\t\n\x0c\r "
DELIMITERS = b"()<>[]{}/%"


class PDFError(ValueError):
    """Файл не PDF, повреждён до нечитаемости или зашифрован."""


class EncryptedPDFError(PDFError):
    """Файл под паролем — разбирать нечего, пока его не снимут."""


@dataclass(slots=True)
class StreamObject:
    """Поток: словарь и ещё не распакованные байты.

    Распаковку держим ленивой: в документе на 500 страниц потоков тысячи, а
    развернуть нужно только контент тех страниц, до которых дошла очередь.
    """

    info: dict
    raw: bytes
    doc: Document | None = None
    _decoded: bytes | None = field(default=None, repr=False)

    def filters(self) -> list[str]:
        raw = self.info.get("Filter")
        if self.doc is not None:
            raw = self.doc.resolve(raw)
        if raw is None:
            return []
        if isinstance(raw, Name):
            return [raw.value]
        if isinstance(raw, list):
            out = []
            for item in raw:
                item = self.doc.resolve(item) if self.doc else item
                if isinstance(item, Name):
                    out.append(item.value)
            return out
        return []

    def params(self) -> list[dict]:
        raw = self.info.get("DecodeParms", self.info.get("DP"))
        if self.doc is not None:
            raw = self.doc.resolve(raw)
        if isinstance(raw, dict):
            return [self._plain(raw)]
        if isinstance(raw, list):
            out = []
            for item in raw:
                item = self.doc.resolve(item) if self.doc else item
                out.append(self._plain(item) if isinstance(item, dict) else {})
            return out
        return []

    def _plain(self, source: dict) -> dict:
        """Словарь параметров с раскрытыми ссылками и обычными ключами."""
        out = {}
        for key, value in source.items():
            name = key.value if isinstance(key, Name) else str(key)
            value = self.doc.resolve(value) if self.doc else value
            out[name] = value.value if isinstance(value, Name) else value
        return out

    def decoded(self) -> bytes:
        """Развёрнутые данные. Для картинок вернёт как есть — они нам не нужны."""
        if self._decoded is None:
            try:
                payload, _stopped = decode(self.raw, self.filters(), self.params())
            except FilterError:
                payload = b""
            self._decoded = payload
        return self._decoded

    def is_image_data(self) -> bool:
        """Поток с пикселями: распаковывать его нечем и незачем."""
        return any(name in IMAGE_FILTERS for name in self.filters())


class Lexer:
    """Разбор синтаксиса PDF: объекты, а внутри страницы — ещё и операторы."""

    __slots__ = ("data", "pos", "size")

    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.data = data
        self.pos = pos
        self.size = len(data)

    def skip_space(self) -> None:
        data, size = self.data, self.size
        while self.pos < size:
            byte = data[self.pos]
            if byte in WHITESPACE:
                self.pos += 1
            elif byte == 0x25:  # '%' — комментарий до конца строки
                while self.pos < size and data[self.pos] not in b"\r\n":
                    self.pos += 1
            else:
                return

    def read_token(self) -> bytes | None:
        """Следующая «голая» лексема: имя, число, ключевое слово, скобка."""
        self.skip_space()
        if self.pos >= self.size:
            return None
        data = self.data
        start = self.pos
        byte = data[start]
        if byte in b"[]{}":
            self.pos += 1
            return data[start : self.pos]
        if byte == 0x3C:  # '<' — словарь или шестнадцатеричная строка
            if self.pos + 1 < self.size and data[self.pos + 1] == 0x3C:
                self.pos += 2
                return b"<<"
            self.pos += 1
            return b"<"
        if byte == 0x3E:
            if self.pos + 1 < self.size and data[self.pos + 1] == 0x3E:
                self.pos += 2
                return b">>"
            self.pos += 1
            return b">"
        if byte in b"()/":
            self.pos += 1
            return data[start : self.pos]
        while self.pos < self.size:
            byte = data[self.pos]
            if byte in WHITESPACE or byte in DELIMITERS:
                break
            self.pos += 1
        if self.pos == start:  # одиночный разделитель, который не разобрали выше
            self.pos += 1
        return data[start : self.pos]

    def read_name(self) -> Name:
        """Имя после уже съеденного `/`, с раскрытием `#xx`."""
        data = self.data
        start = self.pos
        while self.pos < self.size:
            byte = data[self.pos]
            if byte in WHITESPACE or byte in DELIMITERS:
                break
            self.pos += 1
        raw = data[start : self.pos]
        if b"#" in raw:
            out = bytearray()
            index = 0
            while index < len(raw):
                if raw[index] == 0x23 and index + 2 < len(raw):
                    try:
                        out.append(int(raw[index + 1 : index + 3], 16))
                        index += 3
                        continue
                    except ValueError:
                        pass
                out.append(raw[index])
                index += 1
            raw = bytes(out)
        return Name(raw.decode("latin-1"))

    def read_literal_string(self) -> bytes:
        """Строка в круглых скобках; `(` уже съедена."""
        data = self.data
        out = bytearray()
        depth = 1
        while self.pos < self.size:
            byte = data[self.pos]
            self.pos += 1
            if byte == 0x5C:  # обратная косая
                if self.pos >= self.size:
                    break
                escaped = data[self.pos]
                self.pos += 1
                simple = {
                    0x6E: 0x0A,  # n
                    0x72: 0x0D,  # r
                    0x74: 0x09,  # t
                    0x62: 0x08,  # b
                    0x66: 0x0C,  # f
                }.get(escaped)
                if simple is not None:
                    out.append(simple)
                elif escaped in b"()\\":
                    out.append(escaped)
                elif 0x30 <= escaped <= 0x37:  # восьмеричный код
                    digits = chr(escaped)
                    for _ in range(2):
                        if self.pos < self.size and 0x30 <= data[self.pos] <= 0x37:
                            digits += chr(data[self.pos])
                            self.pos += 1
                    out.append(int(digits, 8) & 0xFF)
                elif escaped == 0x0D:  # перенос строки внутри строки — склейка
                    if self.pos < self.size and data[self.pos] == 0x0A:
                        self.pos += 1
                elif escaped == 0x0A:
                    pass
                else:
                    out.append(escaped)
                continue
            if byte == 0x28:
                depth += 1
            elif byte == 0x29:
                depth -= 1
                if depth == 0:
                    break
            out.append(byte)
        return bytes(out)

    def read_hex_string(self) -> bytes:
        """Строка в угловых скобках; `<` уже съедена."""
        data = self.data
        digits = []
        while self.pos < self.size:
            byte = data[self.pos]
            self.pos += 1
            if byte == 0x3E:
                break
            char = chr(byte)
            if char in "0123456789abcdefABCDEF":
                digits.append(char)
        if len(digits) % 2:
            digits.append("0")
        return bytes.fromhex("".join(digits))


_NUMBER = re.compile(rb"^[+-]?(?:\d+\.?\d*|\.\d+)$")


def _as_number(token: bytes) -> int | float | None:
    if not _NUMBER.match(token):
        # Встречается мусор вроде `--3` или `4.-5` — берём, что разбирается.
        cleaned = re.match(rb"^[+-]*(\d*\.?\d*)", token)
        if not cleaned or not cleaned.group(1).strip(b"."):
            return None
        token = (b"-" if token.startswith(b"-") else b"") + cleaned.group(1)
    text = token.decode("ascii")
    if "." in text:
        try:
            return float(text)
        except ValueError:
            return None
    try:
        return int(text)
    except ValueError:
        return None


class ObjectParser(Lexer):
    """Лексер плюс сборка составных объектов: словарей, массивов, ссылок."""

    def parse(self, doc: Document | None = None) -> object:
        token = self.read_token()
        return self._from_token(token, doc)

    def _from_token(self, token: bytes | None, doc: Document | None) -> object:
        if token is None:
            return None
        if token == b"/":
            return self.read_name()
        if token == b"(":
            return self.read_literal_string()
        if token == b"<":
            return self.read_hex_string()
        if token == b"[":
            items = []
            while True:
                mark = self.pos
                inner = self.read_token()
                if inner is None or inner == b"]":
                    break
                self.pos = mark
                items.append(self.parse(doc))
                if self.pos == mark:  # защита от зацикливания на мусоре
                    self.pos += 1
            return self._fold_refs(items)
        if token == b"<<":
            return self._parse_dict(doc)
        if token == b"true":
            return True
        if token == b"false":
            return False
        if token == b"null":
            return None
        number = _as_number(token)
        if number is not None:
            return self._maybe_ref(number)
        return Keyword(token)

    def _maybe_ref(self, first: int | float) -> object:
        """`12 0 R` — ссылка, `12 0` — просто два числа. Решает третья лексема."""
        if not isinstance(first, int) or first < 0:
            return first
        mark = self.pos
        second = self.read_token()
        if second is not None and _NUMBER.match(second) and b"." not in second:
            third = self.read_token()
            if third == b"R":
                return Ref(first, int(second))
        self.pos = mark
        return first

    @staticmethod
    def _fold_refs(items: list) -> list:
        return items

    def _parse_dict(self, doc: Document | None) -> object:
        out: dict = {}
        while True:
            token = self.read_token()
            if token is None or token == b">>":
                break
            if token != b"/":
                # Ключ обязан быть именем; если это не так — пропускаем значение.
                if token == b"<<":
                    self._parse_dict(doc)
                continue
            key = self.read_name().value
            value = self.parse(doc)
            if isinstance(value, Keyword):
                if value.text == b">>":
                    break
                continue
            out[key] = value
        return self._maybe_stream(out, doc)

    def _maybe_stream(self, info: dict, doc: Document | None) -> object:
        mark = self.pos
        token = self.read_token()
        if token != b"stream":
            self.pos = mark
            return info
        data = self.data
        # После слова stream — ровно один перевод строки (CRLF или LF).
        if self.pos < self.size and data[self.pos] == 0x0D:
            self.pos += 1
        if self.pos < self.size and data[self.pos] == 0x0A:
            self.pos += 1
        start = self.pos
        length = info.get("Length")
        if doc is not None:
            length = doc.resolve(length)
        end = -1
        if isinstance(length, int) and length >= 0 and start + length <= self.size:
            end = start + length
            tail = data[end : end + 20]
            if b"endstream" not in tail:
                # /Length соврала — ищем настоящий конец.
                end = -1
        if end < 0:
            end = data.find(b"endstream", start)
            if end < 0:
                end = self.size
            else:
                while end > start and data[end - 1] in b"\r\n":
                    end -= 1
        self.pos = end
        closing = data.find(b"endstream", end)
        self.pos = closing + 9 if closing >= 0 else self.size
        return StreamObject(info, data[start:end], doc)


@dataclass(frozen=True, slots=True)
class Keyword:
    """Голое слово: оператор страницы (`Tj`, `re`) или `obj`/`endobj`."""

    text: bytes


_OBJ_HEADER = re.compile(rb"(\d{1,10})\s+(\d{1,5})\s+obj\b")


class Document:
    """Открытый PDF: доступ к объектам по номеру и список страниц."""

    def __init__(self, data: bytes) -> None:
        if not data:
            raise PDFError("файл пустой")
        head = data[:1024]
        if b"%PDF-" not in head:
            # Иногда перед заголовком лежит мусор от почтовых шлюзов.
            shift = data.find(b"%PDF-")
            if shift < 0:
                raise PDFError("это не PDF: нет заголовка %PDF-")
            data = data[shift:]
        self.data = data
        self.offsets: dict[int, int] = {}
        self.compressed: dict[int, tuple[int, int]] = {}
        self.trailer: dict = {}
        self._cache: dict[int, object] = {}
        self._objstm: dict[int, dict[int, object]] = {}
        self._scanned = False

        try:
            self._read_xref_chain()
        except (PDFError, ValueError, IndexError, KeyError):
            self.offsets.clear()
            self.compressed.clear()
        if not self.offsets and not self.compressed:
            self._scan_objects()
        if not self.trailer.get("Root"):
            self._find_trailer()
        if self.trailer.get("Encrypt") is not None:
            raise EncryptedPDFError(
                "файл защищён паролем. Снимите защиту в просмотрщике "
                "и загрузите снова."
            )

    # --- таблица объектов -------------------------------------------------

    def _read_xref_chain(self) -> None:
        tail = self.data[-2048:]
        marker = tail.rfind(b"startxref")
        if marker < 0:
            raise PDFError("нет startxref")
        lexer = Lexer(tail, marker + 9)
        token = lexer.read_token()
        start = _as_number(token or b"") if token else None
        if not isinstance(start, int):
            raise PDFError("startxref без смещения")

        seen: set[int] = set()
        queue = [start]
        while queue:
            offset = queue.pop(0)
            if offset in seen or not 0 <= offset < len(self.data):
                continue
            seen.add(offset)
            trailer = self._read_xref_at(offset)
            if trailer is None:
                continue
            for key, value in trailer.items():
                self.trailer.setdefault(key, value)
            for key in ("XRefStm", "Prev"):
                nxt = trailer.get(key)
                if isinstance(nxt, int):
                    queue.append(nxt)

    def _read_xref_at(self, offset: int) -> dict | None:
        lexer = Lexer(self.data, offset)
        mark = lexer.pos
        token = lexer.read_token()
        if token == b"xref":
            return self._read_xref_table(lexer)
        lexer.pos = mark
        parser = ObjectParser(self.data, mark)
        header = parser.read_token()
        if header is None or not _NUMBER.match(header):
            return None
        parser.read_token()  # поколение
        if parser.read_token() != b"obj":
            return None
        obj = parser.parse(self)
        if not isinstance(obj, StreamObject):
            return None
        return self._read_xref_stream(obj)

    def _read_xref_table(self, lexer: Lexer) -> dict | None:
        while True:
            mark = lexer.pos
            token = lexer.read_token()
            if token is None:
                return None
            if token == b"trailer":
                parser = ObjectParser(self.data, lexer.pos)
                trailer = parser.parse(self)
                return trailer if isinstance(trailer, dict) else {}
            first = _as_number(token)
            if not isinstance(first, int):
                lexer.pos = mark
                return {}
            count_token = lexer.read_token()
            count = _as_number(count_token or b"")
            if not isinstance(count, int):
                return {}
            for index in range(count):
                lexer.skip_space()
                entry = self.data[lexer.pos : lexer.pos + 20]
                if len(entry) < 18:
                    return {}
                try:
                    position = int(entry[0:10])
                    kind = entry[17:18]
                except ValueError:
                    return {}
                lexer.pos += 18
                while lexer.pos < lexer.size and self.data[lexer.pos] in b"\r\n ":
                    lexer.pos += 1
                number = first + index
                if kind == b"n" and number not in self.offsets:
                    self.offsets[number] = position

    def _read_xref_stream(self, stream: StreamObject) -> dict:
        info = stream.info
        widths = [self.resolve(w) for w in self.resolve(info.get("W")) or []]
        if len(widths) < 3:
            return {}
        size = self.resolve(info.get("Size")) or 0
        index = self.resolve(info.get("Index")) or [0, size]
        payload = stream.decoded()
        row = sum(int(w) for w in widths)
        if row <= 0:
            return {}
        cursor = 0

        def take(width: int) -> int:
            nonlocal cursor
            if width == 0:
                return -1
            value = int.from_bytes(payload[cursor : cursor + width], "big")
            cursor += width
            return value

        pairs = [(int(index[i]), int(index[i + 1])) for i in range(0, len(index) - 1, 2)]
        for first, count in pairs:
            for shift in range(count):
                if cursor + row > len(payload):
                    break
                kind = take(int(widths[0]))
                second = take(int(widths[1]))
                third = take(int(widths[2]))
                if kind == -1:
                    kind = 1  # ширина 0 означает «тип 1 по умолчанию»
                number = first + shift
                if number in self.offsets or number in self.compressed:
                    continue
                if kind == 1:
                    self.offsets[number] = second
                elif kind == 2:
                    self.compressed[number] = (second, max(third, 0))
        plain = {}
        for key, value in info.items():
            plain[key] = value
        return plain

    def _scan_objects(self) -> None:
        """Аварийный разбор: пройти по файлу и запомнить все `N G obj`."""
        if self._scanned:
            return
        self._scanned = True
        for match in _OBJ_HEADER.finditer(self.data):
            number = int(match.group(1))
            # Побеждает последнее вхождение: правки дописываются в конец файла.
            self.offsets[number] = match.start()
        # Объекты внутри ObjStm тоже надо достать — иначе потеряем страницы.
        for number in list(self.offsets):
            obj = self.get(number)
            if isinstance(obj, StreamObject):
                kind = self.resolve(obj.info.get("Type"))
                if isinstance(kind, Name) and kind.value == "ObjStm":
                    for inner in self._load_objstm(number):
                        self.compressed.setdefault(inner, (number, 0))

    def _find_trailer(self) -> None:
        """Найти /Root, когда трейлер потерян или его нет вовсе."""
        for match in re.finditer(rb"trailer", self.data):
            parser = ObjectParser(self.data, match.end())
            trailer = parser.parse(self)
            if isinstance(trailer, dict) and trailer.get("Root") is not None:
                for key, value in trailer.items():
                    self.trailer.setdefault(key, value)
        if self.trailer.get("Root") is not None:
            return
        self._scan_objects()
        for number in sorted(self.offsets) + sorted(self.compressed):
            obj = self.get(number)
            info = obj.info if isinstance(obj, StreamObject) else obj
            if isinstance(info, dict):
                kind = self.resolve(info.get("Type"))
                if isinstance(kind, Name) and kind.value == "Catalog":
                    self.trailer["Root"] = Ref(number, 0)
                    return
        raise PDFError("в файле нет каталога документа — он повреждён")

    # --- доступ к объектам ------------------------------------------------

    def get(self, number: int) -> object:
        if number in self._cache:
            return self._cache[number]
        self._cache[number] = None  # заглушка от бесконечной рекурсии
        value: object = None
        offset = self.offsets.get(number)
        if offset is not None:
            value = self._parse_at(offset, number)
        if value is None and number in self.compressed:
            container, _index = self.compressed[number]
            value = self._load_objstm(container).get(number)
        if value is None and not self._scanned:
            self._scan_objects()
            offset = self.offsets.get(number)
            if offset is not None:
                value = self._parse_at(offset, number)
            if value is None and number in self.compressed:
                container, _index = self.compressed[number]
                value = self._load_objstm(container).get(number)
        self._cache[number] = value
        return value

    def _parse_at(self, offset: int, expected: int) -> object:
        if not 0 <= offset < len(self.data):
            return None
        parser = ObjectParser(self.data, offset)
        token = parser.read_token()
        number = _as_number(token or b"") if token else None
        if number != expected:
            # Смещение сбито — ищем заголовок объекта поблизости.
            window = self.data[max(0, offset - 64) : offset + 512]
            for match in _OBJ_HEADER.finditer(window):
                if int(match.group(1)) == expected:
                    parser = ObjectParser(self.data, max(0, offset - 64) + match.end())
                    return parser.parse(self)
            return None
        parser.read_token()  # поколение
        if parser.read_token() != b"obj":
            return None
        value = parser.parse(self)
        return None if isinstance(value, Keyword) else value

    def _load_objstm(self, number: int) -> dict[int, object]:
        cached = self._objstm.get(number)
        if cached is not None:
            return cached
        self._objstm[number] = {}
        offset = self.offsets.get(number)
        stream = self._parse_at(offset, number) if offset is not None else None
        if not isinstance(stream, StreamObject):
            return {}
        payload = stream.decoded()
        count = self.resolve(stream.info.get("N")) or 0
        first = self.resolve(stream.info.get("First")) or 0
        header = Lexer(payload, 0)
        pairs = []
        for _ in range(int(count)):
            num = _as_number(header.read_token() or b"")
            pos = _as_number(header.read_token() or b"")
            if not isinstance(num, int) or not isinstance(pos, int):
                break
            pairs.append((num, pos))
        out: dict[int, object] = {}
        for num, pos in pairs:
            parser = ObjectParser(payload, int(first) + pos)
            value = parser.parse(self)
            if not isinstance(value, Keyword):
                out[num] = value
        self._objstm[number] = out
        return out

    def resolve(self, value: object, depth: int = 0) -> object:
        """Развернуть ссылку до настоящего объекта."""
        while isinstance(value, Ref) and depth < 32:
            value = self.get(value.num)
            depth += 1
        return value

    def dict_get(self, source: object, *keys: str, default: object = None) -> object:
        """Значение по цепочке ключей с раскрытием ссылок на каждом шаге."""
        current = self.resolve(source)
        for key in keys:
            if isinstance(current, StreamObject):
                current = current.info
            if not isinstance(current, dict):
                return default
            current = self.resolve(current.get(key))
        return default if current is None else current

    # --- страницы ---------------------------------------------------------

    @property
    def catalog(self) -> dict:
        root = self.resolve(self.trailer.get("Root"))
        return root if isinstance(root, dict) else {}

    def pages(self) -> list[Page]:
        """Страницы по порядку, с унаследованными ресурсами и размерами."""
        root = self.resolve(self.catalog.get("Pages"))
        found: list[Page] = []
        seen: set[int] = set()
        inherit = ("Resources", "MediaBox", "CropBox", "Rotate")

        def walk(node: object, carried: dict, depth: int, ref: Ref | None = None) -> None:
            if depth > 64 or len(found) > 20000:
                return
            if isinstance(node, Ref):
                if node.num in seen:
                    return
                seen.add(node.num)
                ref = node
                node = self.resolve(node)
            if not isinstance(node, dict):
                return
            passed = dict(carried)
            for key in inherit:
                if node.get(key) is not None:
                    passed[key] = node[key]
            kids = self.resolve(node.get("Kids"))
            kind = self.resolve(node.get("Type"))
            is_page = isinstance(kind, Name) and kind.value == "Page"
            if isinstance(kids, list) and not is_page:
                for kid in kids:
                    walk(kid, passed, depth + 1)
                return
            merged = dict(node)
            for key, value in passed.items():
                merged.setdefault(key, value)
            found.append(Page(self, merged, len(found), ref))

        start = self.catalog.get("Pages")
        walk(root, {}, 0, start if isinstance(start, Ref) else None)
        if not found:
            # Дерево страниц сломано — соберём страницы перебором объектов.
            self._scan_objects()
            for number in sorted(self.offsets):
                obj = self.get(number)
                if isinstance(obj, dict):
                    kind = self.resolve(obj.get("Type"))
                    if isinstance(kind, Name) and kind.value == "Page":
                        found.append(Page(self, obj, len(found), Ref(number, 0)))
        return found


@dataclass(slots=True)
class Page:
    """Одна страница: её словарь, размеры и содержимое."""

    doc: Document
    info: dict
    index: int
    ref: Ref | None = None  # номер объекта страницы: по нему её заменяют в копии

    @property
    def box(self) -> tuple[float, float, float, float]:
        """Видимая область в пунктах: (x0, y0, x1, y1)."""
        raw = self.doc.resolve(self.info.get("CropBox")) or self.doc.resolve(
            self.info.get("MediaBox")
        )
        values: list[float] = []
        if isinstance(raw, list) and len(raw) >= 4:
            for item in raw[:4]:
                item = self.doc.resolve(item)
                values.append(float(item) if isinstance(item, (int, float)) else 0.0)
        if len(values) != 4:
            values = [0.0, 0.0, 595.276, 841.89]  # A4 по умолчанию
        x0, y0, x1, y1 = values
        return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))

    @property
    def width(self) -> float:
        x0, _y0, x1, _y1 = self.box
        return x1 - x0

    @property
    def height(self) -> float:
        _x0, y0, _x1, y1 = self.box
        return y1 - y0

    @property
    def rotation(self) -> int:
        raw = self.doc.resolve(self.info.get("Rotate"))
        try:
            return int(raw) % 360 // 90 * 90
        except (TypeError, ValueError):
            return 0

    @property
    def resources(self) -> dict:
        raw = self.doc.resolve(self.info.get("Resources"))
        return raw if isinstance(raw, dict) else {}

    def content(self) -> bytes:
        """Все потоки содержимого страницы, склеенные в один."""
        raw = self.doc.resolve(self.info.get("Contents"))
        parts: list[bytes] = []
        if isinstance(raw, StreamObject):
            parts.append(raw.decoded())
        elif isinstance(raw, list):
            for item in raw:
                item = self.doc.resolve(item)
                if isinstance(item, StreamObject):
                    parts.append(item.decoded())
        return b"\n".join(parts)


def load(data: bytes) -> Document:
    return Document(data)
