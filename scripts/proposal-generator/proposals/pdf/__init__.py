"""Свой мини-движок PDF: без внешних библиотек, но с кириллицей и логотипами.

    from proposals.pdf import Document, find_family

    doc = Document(find_family(), title="Коммерческое предложение")
    page = doc.new_page()
    page.text(50, 50, "Привет", size=18, style="bold")
    Path("out.pdf").write_bytes(doc.render())
"""

from .document import (
    A4,
    BOLD,
    BOLD_ITALIC,
    ITALIC,
    MM,
    REGULAR,
    Color,
    Document,
    Image,
    Page,
    mix,
    readable_on,
    rgb,
)
from .fonts import FontError, FontFamily, find_family
from .images import ImageError, load_image

__all__ = [
    "A4",
    "BOLD",
    "BOLD_ITALIC",
    "ITALIC",
    "MM",
    "REGULAR",
    "Color",
    "Document",
    "FontError",
    "FontFamily",
    "Image",
    "ImageError",
    "Page",
    "find_family",
    "load_image",
    "mix",
    "readable_on",
    "rgb",
]
