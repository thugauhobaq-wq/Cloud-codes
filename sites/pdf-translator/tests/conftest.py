"""Общие приспособления для тестов.

Главное здесь — сборщики PDF. Настоящих файлов в репозитории нет намеренно:
проверять надо не «работает на этом одном документе», а работу с устройством
формата — обычной таблицей xref, таблицей-потоком, объектами внутри ObjStm.
Такие файлы проще собрать здесь, чем искать подходящие в природе.
"""

from __future__ import annotations

import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pdftr.pdf.embed import FontError, FontFace, find_family, load
from pdftr.pdf.objects import Name, PDFFile, Stream


@pytest.fixture(scope="session")
def family():
    """Семейство шрифтов из системы.

    Без шрифта с кириллицей печатать перевод нечем, поэтому такие тесты
    пропускаем, а не роняем: на голом образе CI шрифтов может не быть.
    """
    try:
        return find_family()
    except FontError as error:
        pytest.skip(str(error))


class Builder:
    """Маленький сборщик PDF: ровно столько, сколько нужно тестам."""

    def __init__(self, family, width: float = 595.0, height: float = 842.0) -> None:
        self.pdf = PDFFile()
        self.face = FontFace(
            key="F1", ttf=load(family.path_for(False, False)), ps_name="AAAAAA+Test"
        )
        self.font_ref = self.pdf.reserve()
        self.width = width
        self.height = height
        self.pages: list[bytes] = []

    def page(self, parts: list[str]) -> None:
        self.pages.append("\n".join(parts).encode("latin-1", "replace"))

    def text(self, x: float, y: float, text: str, size: float = 10.0) -> str:
        """Строка на странице. `y` считается снизу, как в самом PDF."""
        return (
            f"BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm "
            f"<{self.face.encode(text).hex()}> Tj ET"
        )

    def paragraph(
        self, x: float, y: float, lines: list[str], size: float = 10.0, leading: float = 12.0
    ) -> list[str]:
        return [
            self.text(x, y - index * leading, line, size) for index, line in enumerate(lines)
        ]

    def render(self) -> bytes:
        pages_ref = self.pdf.reserve()
        kids = []
        for content in self.pages:
            stream = self.pdf.add(Stream(content))
            kids.append(
                self.pdf.add(
                    {
                        "Type": Name("Page"),
                        "Parent": pages_ref,
                        "MediaBox": [0, 0, self.width, self.height],
                        "Resources": {"Font": {"F1": self.font_ref}},
                        "Contents": stream,
                    }
                )
            )
        self.pdf.assign(
            pages_ref, {"Type": Name("Pages"), "Kids": kids, "Count": len(kids)}
        )
        root = self.pdf.add({"Type": Name("Catalog"), "Pages": pages_ref})
        self.face.write(self.pdf, self.font_ref)
        return self.pdf.build(root)


@pytest.fixture()
def builder(family):
    return lambda **kwargs: Builder(family, **kwargs)


@pytest.fixture()
def simple_pdf(family):
    """Одна страница с заголовком, абзацем в три строки и подписью."""
    doc = Builder(family)
    doc.page(
        [
            doc.text(60, 780, "Annual Report", 18),
            *doc.paragraph(
                60,
                740,
                [
                    "The system consists of several independent",
                    "services that communicate over a message bus.",
                    "Each service owns its data.",
                ],
            ),
            doc.text(60, 60, "page 1", 8),
        ]
    )
    return doc.render()


@pytest.fixture()
def two_columns_pdf(family):
    """Заголовок во всю ширину и под ним две колонки — проверка разрезания."""
    doc = Builder(family)
    left = ["Left column line one here", "left column line two here", "left column three"]
    right = ["Right column line one", "right column line two", "right column three"]
    doc.page(
        [
            doc.text(60, 780, "Wide heading across the page", 16),
            *doc.paragraph(60, 730, left),
            *doc.paragraph(330, 730, right),
        ]
    )
    return doc.render()


def as_xref_stream(data: bytes) -> bytes:
    """Пересобрать файл так, чтобы таблица объектов лежала потоком (PDF 1.5+).

    Это второй из двух способов записать xref, и в современных документах он
    встречается чаще первого. Разбирать надо оба.
    """
    from pdftr.pdf.reader import Document

    doc = Document(data)
    out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    numbers = sorted(doc.offsets)
    offsets: dict[int, int] = {}
    for number in numbers:
        obj = doc.get(number)
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode("ascii")
        out += _serialize_source(obj)
        out += b"\nendobj\n"

    xref_number = (numbers[-1] if numbers else 0) + 1
    xref_at = len(out)
    rows = bytearray()
    size = xref_number + 1
    for number in range(size):
        if number == 0:
            rows += bytes([0]) + (0).to_bytes(4, "big") + (65535).to_bytes(2, "big")
        elif number == xref_number:
            rows += bytes([1]) + xref_at.to_bytes(4, "big") + (0).to_bytes(2, "big")
        else:
            rows += bytes([1]) + offsets.get(number, 0).to_bytes(4, "big") + (0).to_bytes(2, "big")
    payload = zlib.compress(bytes(rows), 9)
    root = doc.trailer["Root"]
    out += f"{xref_number} 0 obj\n".encode("ascii")
    out += (
        b"<< /Type /XRef /Size " + str(size).encode()
        + b" /W [1 4 2] /Root " + f"{root.num} {root.gen} R".encode()
        + b" /Filter /FlateDecode /Length " + str(len(payload)).encode() + b" >>\nstream\n"
        + payload + b"\nendstream\nendobj\n"
    )
    out += f"startxref\n{xref_at}\n%%EOF\n".encode("ascii")
    return bytes(out)


def _serialize_source(obj: object) -> bytes:
    """Объект из прочитанного документа — обратно в байты."""
    from pdftr.pdf.objects import serialize
    from pdftr.pdf.reader import StreamObject

    if isinstance(obj, StreamObject):
        info = {key: value for key, value in obj.info.items() if key != "Length"}
        return Stream(obj.raw, info, compress=False).serialize()
    return serialize(obj)


class FakeTranslator:
    """Переводчик без сети: заворачивает текст, считая обращения."""

    def __init__(self, prefix: str = "»", fail_after: int | None = None) -> None:
        self.prefix = prefix
        self.calls = 0
        self.texts: list[str] = []
        self.fail_after = fail_after

    def translate(self, text: str, source: str, target: str) -> tuple[str, str]:
        translated, detected = self.translate_batch([text], source, target)
        return translated[0], detected

    def translate_batch(self, texts, source, target):
        from pdftr.translate.google import TranslationError

        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise TranslationError("хватит")
        self.texts.extend(texts)
        return [f"{self.prefix}{text}" for text in texts], "en"
