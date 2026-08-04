"""Перевод больших PDF с сохранением вёрстки.

    python -m pdftr serve                 # приложение для телефона
    python -m pdftr translate книга.pdf --to ru
"""

from .pipeline import Result, translate_document

__all__ = ["Result", "translate_document"]
__version__ = "1.0.0"
