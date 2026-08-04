"""Демо-предложение и рисованный логотип — чтобы первый запуск что-то показывал.

Логотип генерируется кодом: приятнее раздавать репозиторий без бинарников, а
заодно это честный тест вставки PNG с прозрачностью.
"""

from __future__ import annotations

import struct
import zlib
from datetime import date

from .models import VAT_NONE, Bank, LineItem, Party, Proposal, Section


def demo_logo(text: str = "ТУ", color: tuple[int, int, int] = (32, 94, 255)) -> bytes:
    """PNG-заглушка: скруглённый квадрат акцентного цвета с двумя буквами.

    Буквы рисуем блочным шрифтом 5×7 — тащить растеризатор ради демо незачем.
    """
    size = 256
    pixels = bytearray(size * size * 4)
    radius = 52

    for y in range(size):
        for x in range(size):
            # Скругление: считаем расстояние до ближайшего центра угловой дуги.
            cx = min(max(x, radius), size - radius)
            cy = min(max(y, radius), size - radius)
            distance = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            alpha = 255 if distance <= radius - 1 else (0 if distance > radius else 128)
            at = (y * size + x) * 4
            pixels[at : at + 3] = bytes(color)
            pixels[at + 3] = alpha

    _stamp_text(pixels, size, text[:2].upper())
    return _write_png(pixels, size, size)


_GLYPHS: dict[str, tuple[str, ...]] = {
    "А": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "Б": ("11111", "10000", "11110", "10001", "10001", "10001", "11110"),
    "В": ("11110", "10001", "11110", "10001", "10001", "10001", "11110"),
    "Г": ("11111", "10000", "10000", "10000", "10000", "10000", "10000"),
    "Д": ("00111", "01001", "01001", "01001", "01001", "11111", "10001"),
    "К": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "Л": ("00111", "01001", "01001", "01001", "01001", "01001", "10001"),
    "М": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "Н": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "О": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "П": ("11111", "10001", "10001", "10001", "10001", "10001", "10001"),
    "Р": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "С": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "Т": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "У": ("10001", "10001", "10001", "01111", "00001", "00001", "01110"),
    "Ф": ("11111", "00100", "01110", "10101", "01110", "00100", "00100"),
    "Ц": ("10010", "10010", "10010", "10010", "10010", "11111", "00001"),
    "Ч": ("10001", "10001", "10001", "01111", "00001", "00001", "00001"),
    "Ш": ("10101", "10101", "10101", "10101", "10101", "10101", "11111"),
    "Э": ("11110", "00001", "00001", "01111", "00001", "00001", "11110"),
    "Ю": ("10010", "10101", "10101", "11101", "10101", "10101", "10010"),
    "Я": ("01111", "10001", "10001", "01111", "00101", "01001", "10001"),
    "?": ("01110", "10001", "00001", "00110", "00100", "00000", "00100"),
}


def _stamp_text(pixels: bytearray, size: int, text: str) -> None:
    """Нарисовать 1–2 буквы белым по центру."""
    scale = 12
    glyphs = [_GLYPHS.get(char, _GLYPHS["?"]) for char in text]
    gap = scale
    total_width = sum(5 * scale for _ in glyphs) + gap * (len(glyphs) - 1)
    start_x = (size - total_width) // 2
    start_y = (size - 7 * scale) // 2

    for index, glyph in enumerate(glyphs):
        origin = start_x + index * (5 * scale + gap)
        for row, bits in enumerate(glyph):
            for column, bit in enumerate(bits):
                if bit != "1":
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        x = origin + column * scale + dx
                        y = start_y + row * scale + dy
                        if 0 <= x < size and 0 <= y < size:
                            at = (y * size + x) * 4
                            pixels[at : at + 4] = b"\xff\xff\xff\xff"


def _write_png(pixels: bytearray, width: int, height: int) -> bytes:
    """Собрать RGBA-PNG без фильтрации строк."""
    raw = bytearray()
    stride = width * 4
    for row in range(height):
        raw.append(0)  # фильтр None
        raw += pixels[row * stride : (row + 1) * stride]

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def demo_proposal(today: date | None = None) -> Proposal:
    """Заполненное предложение из жизни небольшой студии."""
    issued = today or date.today()
    proposal = Proposal(
        number="47",
        issued_at=issued,
        valid_days=14,
        subject="Разработка сайта-каталога и подключение онлайн-оплаты",
        intro=(
            "Спасибо за встречу. Ниже — состав работ, сроки и стоимость по итогам "
            "обсуждения. Запускаем каталог на 300 позиций, переносим товары из "
            "выгрузки 1С и подключаем оплату картой. Всё, что не вошло в смету, "
            "обсуждаем отдельно — без сюрпризов в счёте."
        ),
        sender=Party(
            name="Студия «Тихий Угол»",
            person="Анна Северова",
            position="Руководитель проектов",
            phone="+7 999 123-45-67",
            email="hello@tihiy-ugol.ru",
            site="tihiy-ugol.ru",
            details="ИП Северова Анна Владимировна, ОГРНИП 321774600123456",
            inn="771234567890",
            # Реквизиты в примере настоящие по форме, но выдуманные по содержанию:
            # так «proposals invoice» работает сразу после установки.
            bank=Bank(
                name="АО «Тестовый Банк», г. Москва",
                bik="044525974",
                account="40802810100000001234",
                corr_account="30101810145250000974",
            ),
        ),
        client=Party(
            name="ООО «Ромашка»",
            person="Игорь Лапин",
            position="Коммерческий директор",
            email="lapin@romashka.example",
            address="г. Казань, ул. Баумана, 12, офис 305",
            inn="1655123456",
            kpp="165501001",
        ),
        items=[
            LineItem(
                title="Дизайн-концепция и макеты",
                description=(
                    "Главная, каталог, карточка товара, корзина и оформление заказа. "
                    "Две итерации правок включены."
                ),
                quantity=1,
                unit="проект",
                price=120000,
            ),
            LineItem(
                title="Вёрстка и сборка витрины",
                description="Адаптив под мобильные, поддержка Safari и Chrome последних версий.",
                quantity=1,
                unit="проект",
                price=95000,
            ),
            LineItem(
                title="Перенос каталога из 1С",
                description="Разбор выгрузки, сопоставление категорий, загрузка изображений.",
                quantity=300,
                unit="позиция",
                price=140,
                discount_percent=10,
            ),
            LineItem(
                title="Подключение эквайринга",
                description="ЮKassa: оплата картой и СБП, чеки по 54-ФЗ.",
                quantity=1,
                unit="шт.",
                price=28000,
            ),
            LineItem(
                title="Сопровождение после запуска",
                description="Мелкие правки и ответы на вопросы, до 8 часов в месяц.",
                quantity=3,
                unit="мес.",
                price=18000,
            ),
        ],
        sections=[
            Section(
                title="Что вы получите",
                body=(
                    "Работающий каталог с оплатой, доступ в админку и короткую "
                    "инструкцию для контент-менеджера. Исходники макетов и код "
                    "передаём вам — привязки к студии нет."
                ),
            ),
            Section(
                title="Как идёт работа",
                body=(
                    "Неделя 1–2: дизайн и согласование. Неделя 3–5: вёрстка и перенос "
                    "каталога. Неделя 6: эквайринг, тестирование, запуск. "
                    "Каждую пятницу — короткий отчёт о том, что сделано."
                ),
            ),
        ],
        currency="RUB",
        vat_mode=VAT_NONE,
        discount_percent=5,
        lead_time="6 недель с момента предоплаты",
        payment_terms="50% предоплата, 50% по факту запуска",
        notes=(
            "Стоимость хостинга и домена в смету не входит — оплачивается напрямую "
            "провайдеру, ориентировочно 900 ₽ в месяц."
        ),
        accent="#1f5eff",
        template="classic",
    )
    proposal.sender.logo = demo_logo("ТУ", (32, 94, 255))
    proposal.client.logo = demo_logo("Р", (214, 68, 68))
    return proposal
