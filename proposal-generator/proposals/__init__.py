"""Генератор коммерческих предложений: форма или JSON → PDF с логотипом клиента.

Без внешних зависимостей: PDF, включая встраивание шрифтов и картинок,
собирается кодом из пакета `proposals.pdf`.
"""

from .models import LineItem, Party, Proposal, Section
from .template import render

__all__ = ["LineItem", "Party", "Proposal", "Section", "render"]
__version__ = "0.1.0"
