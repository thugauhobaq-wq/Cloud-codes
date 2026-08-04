"""Низкий уровень PDF: объекты, ссылки, потоки и сборка файла.

Здесь нет ничего про коммерческие предложения — только формат PDF 1.7:
как сериализовать словарь, как посчитать таблицу xref, как упаковать поток.
Всё, что выше (шрифты, картинки, вёрстка), пользуется этим модулем.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field


def fmt_number(value: float) -> str:
    """Число в записи PDF: без экспоненты и без хвостовых нулей.

    PDF не понимает `1e-05`, а координаты после пересчёта пунктов почти всегда
    иррациональные, поэтому округляем до четырёх знаков — этого хватает при
    разрешении печати 1200 dpi (0.06 пункта).
    """
    if isinstance(value, int):
        return str(value)
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-", "-0") else "0"


class Name:
    """Имя PDF (`/Type`, `/Font`). Отдельный тип, чтобы не путать со строкой."""

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value

    def __repr__(self) -> str:  # pragma: no cover - отладочное
        return f"Name({self.value!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Name) and other.value == self.value

    def __hash__(self) -> int:
        return hash(("Name", self.value))


class TextString(str):
    """Строка, которую увидит человек: заголовок документа, автор.

    Сериализуется как UTF-16BE с BOM — иначе кириллица в свойствах файла
    превращается в мусор.
    """

    __slots__ = ()


class Raw:
    """Готовые байты, которые надо положить в файл как есть."""

    __slots__ = ("data",)

    def __init__(self, data: bytes) -> None:
        self.data = data


@dataclass(frozen=True, slots=True)
class Ref:
    """Косвенная ссылка `12 0 R`."""

    num: int
    gen: int = 0


@dataclass(slots=True)
class Stream:
    """Поток: словарь + данные. Сжатие включено по умолчанию."""

    data: bytes
    extra: dict[str, object] = field(default_factory=dict)
    compress: bool = True

    def serialize(self) -> bytes:
        payload = self.data
        info: dict[str, object] = dict(self.extra)
        if self.compress and "Filter" not in info:
            packed = zlib.compress(payload, 9)
            # На крошечных потоках zlib только добавляет байты.
            if len(packed) < len(payload):
                payload = packed
                info["Filter"] = Name("FlateDecode")
        info["Length"] = len(payload)
        return serialize(info) + b"\nstream\n" + payload + b"\nendstream"


_ESCAPE = {
    ord("("): b"\\(",
    ord(")"): b"\\)",
    ord("\\"): b"\\\\",
    ord("\r"): b"\\r",
    ord("\n"): b"\\n",
    ord("\t"): b"\\t",
}


def _literal_string(raw: bytes) -> bytes:
    out = bytearray(b"(")
    for byte in raw:
        replacement = _ESCAPE.get(byte)
        if replacement is not None:
            out += replacement
        elif 32 <= byte < 127:
            out.append(byte)
        else:
            out += f"\\{byte:03o}".encode("ascii")
    out += b")"
    return bytes(out)


def serialize(obj: object) -> bytes:
    """Питоновское значение → байты PDF."""
    if obj is None:
        return b"null"
    if isinstance(obj, Raw):
        return obj.data
    if isinstance(obj, bool):
        return b"true" if obj else b"false"
    if isinstance(obj, Name):
        # Пробелы и спецсимволы в именах экранируются через #xx.
        safe = "".join(
            ch if ch.isalnum() or ch in "-_.+" else f"#{ord(ch):02X}" for ch in obj.value
        )
        return b"/" + safe.encode("ascii")
    if isinstance(obj, Ref):
        return f"{obj.num} {obj.gen} R".encode("ascii")
    if isinstance(obj, TextString):
        return b"<" + (b"\xfe\xff" + obj.encode("utf-16-be")).hex().upper().encode("ascii") + b">"
    if isinstance(obj, (int, float)):
        return fmt_number(obj).encode("ascii")
    if isinstance(obj, bytes):
        return _literal_string(obj)
    if isinstance(obj, str):
        return _literal_string(obj.encode("latin-1", "replace"))
    if isinstance(obj, (list, tuple)):
        return b"[" + b" ".join(serialize(item) for item in obj) + b"]"
    if isinstance(obj, dict):
        parts = []
        for key, value in obj.items():
            name = key if isinstance(key, Name) else Name(str(key))
            parts.append(serialize(name) + b" " + serialize(value))
        return b"<<" + b" ".join(parts) + b">>"
    if isinstance(obj, Stream):
        return obj.serialize()
    raise TypeError(f"нечего писать в PDF для {type(obj).__name__}")


class PDFFile:
    """Накопитель объектов и сборщик итогового файла."""

    def __init__(self) -> None:
        self._objects: list[object] = []

    def reserve(self) -> Ref:
        """Выдать номер заранее — нужно для циклических ссылок (страница ↔ дерево)."""
        self._objects.append(None)
        return Ref(len(self._objects))

    def assign(self, ref: Ref, obj: object) -> Ref:
        self._objects[ref.num - 1] = obj
        return ref

    def add(self, obj: object) -> Ref:
        self._objects.append(obj)
        return Ref(len(self._objects))

    def build(self, root: Ref, info: Ref | None = None, doc_id: bytes = b"") -> bytes:
        out = bytearray(b"%PDF-1.7\n")
        # Комментарий с байтами >127: подсказка утилитам, что файл бинарный.
        out += b"%\xe2\xe3\xcf\xd3\n"

        offsets = [0] * (len(self._objects) + 1)
        for index, obj in enumerate(self._objects, start=1):
            if obj is None:
                raise ValueError(f"объект {index} зарезервирован, но не заполнен")
            offsets[index] = len(out)
            out += f"{index} 0 obj\n".encode("ascii")
            out += serialize(obj)
            out += b"\nendobj\n"

        xref_at = len(out)
        count = len(self._objects) + 1
        out += f"xref\n0 {count}\n".encode("ascii")
        out += b"0000000000 65535 f \n"
        for index in range(1, count):
            out += f"{offsets[index]:010d} 00000 n \n".encode("ascii")

        trailer: dict[str, object] = {"Size": count, "Root": root}
        if info is not None:
            trailer["Info"] = info
        if doc_id:
            digest = doc_id.hex().upper().encode("ascii")
            trailer["ID"] = [Raw(b"<" + digest + b">"), Raw(b"<" + digest + b">")]
        out += b"trailer\n" + serialize(trailer)
        out += f"\nstartxref\n{xref_at}\n%%EOF\n".encode("ascii")
        return bytes(out)
