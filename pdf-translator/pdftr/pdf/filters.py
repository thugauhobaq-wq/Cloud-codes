"""Распаковка потоков PDF.

Содержимое страницы, шрифты и карты символов лежат в потоках, и почти всегда
сжатыми. Чтобы прочитать текст, поток надо сначала развернуть — этим и занят
модуль. Картиночные фильтры (DCT, JPX, CCITT, JBIG2) не трогаем: их данные мы
переносим в готовый файл как есть, разбирать пиксели незачем.
"""

from __future__ import annotations

import zlib


class FilterError(ValueError):
    """Поток объявлен сжатым, но развернуть его не получилось."""


# Фильтры, после которых в потоке остаются пиксели, а не разметка. Для нас это
# просто «дальше не распаковываем».
IMAGE_FILTERS = frozenset(
    {"DCTDecode", "DCT", "JPXDecode", "CCITTFaxDecode", "CCF", "JBIG2Decode"}
)


def _flate(data: bytes) -> bytes:
    try:
        return zlib.decompress(data)
    except zlib.error:
        pass
    # Битый хвост — обычная беда PDF-ов из принтеров: поток обрывается без
    # финального блока. decompressobj отдаёт всё, что успел разобрать.
    unpacker = zlib.decompressobj()
    try:
        out = unpacker.decompress(data)
    except zlib.error:
        # Иногда врут и про сам заголовок zlib — пробуем сырой deflate.
        try:
            return zlib.decompressobj(-15).decompress(data)
        except zlib.error as error:
            raise FilterError(f"FlateDecode: {error}") from error
    return out


def _ascii_hex(data: bytes) -> bytes:
    digits = []
    for byte in data:
        char = chr(byte)
        if char == ">":
            break
        if char in "0123456789abcdefABCDEF":
            digits.append(char)
    if len(digits) % 2:
        digits.append("0")  # по стандарту недостающая цифра — ноль
    return bytes.fromhex("".join(digits))


def _ascii85(data: bytes) -> bytes:
    payload = bytes(data)
    if payload.startswith(b"<~"):
        payload = payload[2:]
    end = payload.find(b"~>")
    if end >= 0:
        payload = payload[:end]
    out = bytearray()
    group: list[int] = []
    for byte in payload:
        if byte in b" \t\r\n\f\x00":
            continue
        if byte == ord("z") and not group:
            out += b"\x00\x00\x00\x00"
            continue
        if not 33 <= byte <= 117:
            raise FilterError(f"ASCII85Decode: недопустимый символ {byte!r}")
        group.append(byte - 33)
        if len(group) == 5:
            value = 0
            for digit in group:
                value = value * 85 + digit
            out += value.to_bytes(4, "big")
            group.clear()
    if group:
        missing = 5 - len(group)
        value = 0
        for digit in group + [84] * missing:
            value = value * 85 + digit
        out += value.to_bytes(4, "big")[: 4 - missing]
    return bytes(out)


def _run_length(data: bytes) -> bytes:
    out = bytearray()
    index = 0
    size = len(data)
    while index < size:
        length = data[index]
        index += 1
        if length == 128:
            break
        if length < 128:
            out += data[index : index + length + 1]
            index += length + 1
        else:
            if index >= size:
                break
            out += bytes([data[index]]) * (257 - length)
            index += 1
    return bytes(out)


def _lzw(data: bytes, early: int = 1) -> bytes:
    """LZW из PDF: как в TIFF, с ранним увеличением кода на единицу."""
    table: list[bytes] = [bytes([i]) for i in range(256)] + [b"", b""]
    out = bytearray()
    previous: bytes | None = None
    bits = 9
    buffer = 0
    filled = 0
    for byte in data:
        buffer = (buffer << 8) | byte
        filled += 8
        while filled >= bits:
            filled -= bits
            code = (buffer >> filled) & ((1 << bits) - 1)
            if code == 256:
                table = [bytes([i]) for i in range(256)] + [b"", b""]
                bits = 9
                previous = None
                continue
            if code == 257:
                return bytes(out)
            if previous is None:
                entry = table[code]
            elif code < len(table):
                entry = table[code]
                table.append(previous + entry[:1])
            else:
                entry = previous + previous[:1]
                table.append(entry)
            out += entry
            previous = entry
            limit = len(table) + early
            if limit >= 512 and bits == 9:
                bits = 10
            elif limit >= 1024 and bits == 10:
                bits = 11
            elif limit >= 2048 and bits == 11:
                bits = 12
        buffer &= (1 << filled) - 1
    return bytes(out)


def apply_predictor(data: bytes, params: dict) -> bytes:
    """Обратить предиктор: до сжатия строки данных вычитали друг из друга.

    Так делают с таблицами xref и с картинками — без обратного хода получатся
    случайные байты.
    """
    predictor = int(params.get("Predictor", 1) or 1)
    if predictor <= 1:
        return data
    colors = int(params.get("Colors", 1) or 1)
    bpc = int(params.get("BitsPerComponent", 8) or 8)
    columns = int(params.get("Columns", 1) or 1)
    sample = max(1, (colors * bpc + 7) // 8)
    row_len = (columns * colors * bpc + 7) // 8

    if predictor == 2:
        if bpc != 8:
            # TIFF-предиктор на нецелых байтах встречается только в экзотике.
            return data
        out = bytearray(data)
        for start in range(0, len(out) - row_len + 1, row_len):
            for index in range(sample, row_len):
                out[start + index] = (out[start + index] + out[start + index - sample]) & 0xFF
        return bytes(out)

    # PNG-предикторы: первый байт строки — номер фильтра.
    out = bytearray()
    previous = bytearray(row_len)
    step = row_len + 1
    for start in range(0, len(data), step):
        chunk = data[start : start + step]
        if len(chunk) < 2:
            break
        tag = chunk[0]
        row = bytearray(chunk[1:])
        row += bytes(row_len - len(row))  # оборванная последняя строка
        if tag == 1:
            for i in range(sample, row_len):
                row[i] = (row[i] + row[i - sample]) & 0xFF
        elif tag == 2:
            for i in range(row_len):
                row[i] = (row[i] + previous[i]) & 0xFF
        elif tag == 3:
            for i in range(row_len):
                left = row[i - sample] if i >= sample else 0
                row[i] = (row[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif tag == 4:
            for i in range(row_len):
                left = row[i - sample] if i >= sample else 0
                up = previous[i]
                upleft = previous[i - sample] if i >= sample else 0
                guess = left + up - upleft
                da, db, dc = abs(guess - left), abs(guess - up), abs(guess - upleft)
                if da <= db and da <= dc:
                    nearest = left
                elif db <= dc:
                    nearest = up
                else:
                    nearest = upleft
                row[i] = (row[i] + nearest) & 0xFF
        elif tag != 0:
            raise FilterError(f"неизвестный PNG-предиктор {tag}")
        out += row
        previous = row
    return bytes(out)


def decode(data: bytes, filters: list[str], params: list[dict]) -> tuple[bytes, str | None]:
    """Развернуть поток по цепочке фильтров.

    Возвращает данные и имя фильтра, на котором остановились: для картинок это
    непустое значение — знак, что дальше распаковывать нечем и не нужно.
    """
    payload = data
    for index, name in enumerate(filters):
        if name in IMAGE_FILTERS:
            return payload, name
        options = params[index] if index < len(params) else {}
        options = options or {}
        if name in ("FlateDecode", "Fl"):
            payload = _flate(payload)
        elif name in ("LZWDecode", "LZW"):
            payload = _lzw(payload, int(options.get("EarlyChange", 1) or 0))
        elif name in ("ASCIIHexDecode", "AHx"):
            payload = _ascii_hex(payload)
        elif name in ("ASCII85Decode", "A85"):
            payload = _ascii85(payload)
        elif name in ("RunLengthDecode", "RL"):
            payload = _run_length(payload)
        elif name == "Crypt":
            continue  # /Identity — единственный вариант в незашифрованном файле
        else:
            raise FilterError(f"неизвестный фильтр потока /{name}")
        if options.get("Predictor"):
            payload = apply_predictor(payload, options)
    return payload, None
