"""Модель коммерческого предложения и вся арифметика по нему.

Это единственное место, где считаются суммы. И веб-форма, и CLI, и шаблон PDF
берут числа отсюда — иначе «итого» на экране и «итого» в файле однажды
разойдутся.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from .money import CENTS, amount_in_words, money, quantity

VAT_INCLUDED = "included"
"""НДС уже сидит в цене (обычный режим для российского юрлица на ОСН)."""

VAT_ADDED = "added"
"""НДС начисляется сверху."""

VAT_NONE = "none"
"""Без НДС — УСН, самозанятые, ИП на патенте."""

VAT_MODES = (VAT_NONE, VAT_INCLUDED, VAT_ADDED)


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _digits(value: object) -> str:
    """Номера счетов и ИНН приходят с пробелами из платёжек — чистим."""
    return "".join(char for char in _text(value) if char.isdigit())


@dataclass(slots=True)
class Bank:
    """Банковские реквизиты — то, без чего счёт не оплатят.

    Отдельными полями, а не свободным текстом: в шапке счёта у каждого своя
    клетка, и бухгалтер ищет их глазами всегда на одном месте.
    """

    name: str = ""
    """Название банка."""
    bik: str = ""
    account: str = ""
    """Расчётный счёт получателя."""
    corr_account: str = ""
    """Корреспондентский счёт банка."""

    @classmethod
    def from_dict(cls, raw: dict) -> Bank:
        return cls(
            name=_text(raw.get("name")),
            bik=_digits(raw.get("bik")),
            account=_digits(raw.get("account")),
            corr_account=_digits(raw.get("corr_account")),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "bik": self.bik,
            "account": self.account,
            "corr_account": self.corr_account,
        }

    @property
    def filled(self) -> bool:
        """Хватает ли данных, чтобы платёж вообще прошёл."""
        return bool(self.name and self.bik and self.account)


@dataclass(slots=True)
class Party:
    """Сторона предложения: исполнитель или заказчик."""

    name: str = ""
    person: str = ""
    """ФИО и должность подписанта."""
    position: str = ""
    phone: str = ""
    email: str = ""
    site: str = ""
    address: str = ""
    details: str = ""
    """Прочие реквизиты свободным текстом: ОГРН, номер договора."""
    inn: str = ""
    kpp: str = ""
    bank: Bank = field(default_factory=Bank)
    logo: bytes | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> Party:
        return cls(
            name=_text(raw.get("name")),
            person=_text(raw.get("person")),
            position=_text(raw.get("position")),
            phone=_text(raw.get("phone")),
            email=_text(raw.get("email")),
            site=_text(raw.get("site")),
            address=_text(raw.get("address")),
            details=_text(raw.get("details")),
            inn=_digits(raw.get("inn")),
            kpp=_digits(raw.get("kpp")),
            bank=Bank.from_dict(raw.get("bank") or {}),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "person": self.person,
            "position": self.position,
            "phone": self.phone,
            "email": self.email,
            "site": self.site,
            "address": self.address,
            "details": self.details,
            "inn": self.inn,
            "kpp": self.kpp,
            "bank": self.bank.to_dict(),
        }

    @property
    def contacts(self) -> list[str]:
        """Контакты одной строкой каждый — для шапки и подвала."""
        return [value for value in (self.phone, self.email, self.site) if value]

    @property
    def tax_line(self) -> str:
        """«ИНН 7712345678, КПП 771201001» — как принято писать в счёте."""
        parts = []
        if self.inn:
            parts.append(f"ИНН {self.inn}")
        if self.kpp:
            parts.append(f"КПП {self.kpp}")
        return ", ".join(parts)

    def full_line(self) -> str:
        """Строка «Покупатель»/«Поставщик» счёта: реквизиты, имя, адрес."""
        parts = [value for value in (self.tax_line, self.name, self.address) if value]
        return ", ".join(parts)


@dataclass(slots=True)
class LineItem:
    """Строка сметы."""

    title: str = ""
    description: str = ""
    quantity: Decimal = Decimal("1")
    unit: str = "шт."
    price: Decimal = Decimal("0")
    discount_percent: Decimal = Decimal("0")
    """Скидка на конкретную позицию, поверх общей."""

    def __post_init__(self) -> None:
        # Позицию собирают и руками (`LineItem(price=120000)`), и из формы,
        # где всё приходит строками. Приводим к Decimal в одном месте.
        self.quantity = quantity(self.quantity)
        self.price = money(self.price)
        self.discount_percent = _percent(self.discount_percent)

    @classmethod
    def from_dict(cls, raw: dict) -> LineItem:
        return cls(
            title=_text(raw.get("title")),
            description=_text(raw.get("description")),
            quantity=quantity(raw.get("quantity", 1)),
            unit=_text(raw.get("unit")) or "шт.",
            price=money(raw.get("price", 0)),
            discount_percent=_percent(raw.get("discount_percent", 0)),
        )

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "description": self.description,
            "quantity": str(self.quantity),
            "unit": self.unit,
            "price": str(self.price),
            "discount_percent": str(self.discount_percent),
        }

    @property
    def gross(self) -> Decimal:
        """Количество × цена, до позиционной скидки."""
        return (self.quantity * self.price).quantize(CENTS, rounding=ROUND_HALF_UP)

    @property
    def discount(self) -> Decimal:
        if not self.discount_percent:
            return Decimal("0.00")
        return (self.gross * self.discount_percent / 100).quantize(
            CENTS, rounding=ROUND_HALF_UP
        )

    @property
    def total(self) -> Decimal:
        return self.gross - self.discount

    @property
    def is_empty(self) -> bool:
        return not self.title and not self.description and not self.price


@dataclass(slots=True)
class Section:
    """Произвольный текстовый блок: «Что входит», «Этапы», «О нас»."""

    title: str = ""
    body: str = ""

    @classmethod
    def from_dict(cls, raw: dict) -> Section:
        return cls(title=_text(raw.get("title")), body=_text(raw.get("body")))

    def to_dict(self) -> dict:
        return {"title": self.title, "body": self.body}

    @property
    def is_empty(self) -> bool:
        return not self.title and not self.body


def _percent(value: object) -> Decimal:
    raw = quantity(value)
    return raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _date(value: object, default: date | None = None) -> date | None:
    """Дата из формы: ISO, `31.12.2026` или `31/12/2026`."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    if not text:
        return default
    for pattern in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return default


@dataclass(slots=True)
class Totals:
    """Посчитанные итоги — то, что уходит в блок «К оплате»."""

    subtotal: Decimal
    """Сумма строк уже за вычетом позиционных скидок."""
    item_discount: Decimal
    """Сколько скинуто внутри строк."""
    discount: Decimal
    """Общая скидка на предложение."""
    net: Decimal
    """База после всех скидок."""
    vat: Decimal
    total: Decimal
    currency: str
    vat_mode: str
    vat_rate: Decimal

    @property
    def in_words(self) -> str:
        return amount_in_words(self.total, self.currency)


@dataclass(slots=True)
class Proposal:
    """Коммерческое предложение целиком."""

    number: str = "1"
    issued_at: date = field(default_factory=date.today)
    valid_days: int = 14
    subject: str = ""
    """Тема: «Разработка сайта-каталога»."""
    intro: str = ""
    sender: Party = field(default_factory=Party)
    client: Party = field(default_factory=Party)
    items: list[LineItem] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    currency: str = "RUB"
    vat_mode: str = VAT_NONE
    vat_rate: Decimal = Decimal("20")
    discount_percent: Decimal = Decimal("0")
    lead_time: str = ""
    payment_terms: str = ""
    notes: str = ""
    accent: str = "#1f5eff"
    template: str = "classic"
    show_signature: bool = True

    def __post_init__(self) -> None:
        self.vat_rate = _percent(self.vat_rate)
        self.discount_percent = _percent(self.discount_percent)
        self.currency = (self.currency or "RUB").upper()

    # --- сериализация --------------------------------------------------------

    @classmethod
    def from_dict(cls, raw: dict) -> Proposal:
        issued = _date(raw.get("issued_at"), date.today()) or date.today()
        items = [LineItem.from_dict(item) for item in raw.get("items") or []]
        sections = [Section.from_dict(block) for block in raw.get("sections") or []]
        vat_mode = _text(raw.get("vat_mode")) or VAT_NONE
        if vat_mode not in VAT_MODES:
            vat_mode = VAT_NONE
        proposal = cls(
            number=_text(raw.get("number")) or "1",
            issued_at=issued,
            valid_days=_int(raw.get("valid_days"), 14),
            subject=_text(raw.get("subject")),
            intro=_text(raw.get("intro")),
            sender=Party.from_dict(raw.get("sender") or {}),
            client=Party.from_dict(raw.get("client") or {}),
            items=[item for item in items if not item.is_empty],
            sections=[block for block in sections if not block.is_empty],
            currency=(_text(raw.get("currency")) or "RUB").upper(),
            vat_mode=vat_mode,
            vat_rate=_percent(raw.get("vat_rate", 20)),
            discount_percent=_percent(raw.get("discount_percent", 0)),
            lead_time=_text(raw.get("lead_time")),
            payment_terms=_text(raw.get("payment_terms")),
            notes=_text(raw.get("notes")),
            accent=_text(raw.get("accent")) or "#1f5eff",
            template=_text(raw.get("template")) or "classic",
            show_signature=bool(raw.get("show_signature", True)),
        )
        return proposal

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "issued_at": self.issued_at.isoformat(),
            "valid_days": self.valid_days,
            "subject": self.subject,
            "intro": self.intro,
            "sender": self.sender.to_dict(),
            "client": self.client.to_dict(),
            "items": [item.to_dict() for item in self.items],
            "sections": [block.to_dict() for block in self.sections],
            "currency": self.currency,
            "vat_mode": self.vat_mode,
            "vat_rate": str(self.vat_rate),
            "discount_percent": str(self.discount_percent),
            "lead_time": self.lead_time,
            "payment_terms": self.payment_terms,
            "notes": self.notes,
            "accent": self.accent,
            "template": self.template,
            "show_signature": self.show_signature,
        }

    # --- расчёты -------------------------------------------------------------

    @property
    def valid_until(self) -> date:
        return self.issued_at + timedelta(days=max(self.valid_days, 0))

    def totals(self) -> Totals:
        """Пересчитать всё предложение.

        Порядок важен и повторяет привычный счёт: сначала строки со своими
        скидками, потом общая скидка на остаток, и только затем НДС.
        """
        gross = sum((item.gross for item in self.items), Decimal("0.00"))
        item_discount = sum((item.discount for item in self.items), Decimal("0.00"))
        subtotal = (gross - item_discount).quantize(CENTS, rounding=ROUND_HALF_UP)

        discount = Decimal("0.00")
        if self.discount_percent:
            discount = (subtotal * self.discount_percent / 100).quantize(
                CENTS, rounding=ROUND_HALF_UP
            )
        net = subtotal - discount

        rate = self.vat_rate
        if self.vat_mode == VAT_ADDED:
            vat = (net * rate / 100).quantize(CENTS, rounding=ROUND_HALF_UP)
            total = net + vat
        elif self.vat_mode == VAT_INCLUDED:
            # Выделяем НДС из суммы: net × ставка / (100 + ставка).
            vat = (net * rate / (100 + rate)).quantize(CENTS, rounding=ROUND_HALF_UP)
            total = net
        else:
            vat = Decimal("0.00")
            total = net

        return Totals(
            subtotal=subtotal,
            item_discount=item_discount.quantize(CENTS, rounding=ROUND_HALF_UP),
            discount=discount,
            net=net,
            vat=vat,
            total=total,
            currency=self.currency,
            vat_mode=self.vat_mode,
            vat_rate=rate,
        )

    @property
    def vat_note(self) -> str:
        """Строка про НДС, которую положено писать в документе."""
        if self.vat_mode == VAT_NONE:
            return "НДС не облагается"
        rate = f"{self.vat_rate:.2f}".rstrip("0").rstrip(".").replace(".", ",")
        if self.vat_mode == VAT_INCLUDED:
            return f"В том числе НДС {rate}%"
        return f"НДС {rate}% сверху"

    def validate(self) -> list[str]:
        """Что не заполнено. Не блокирует генерацию — просто предупреждаем."""
        problems: list[str] = []
        if not self.sender.name:
            problems.append("не указан исполнитель")
        if not self.client.name:
            problems.append("не указан заказчик")
        if not self.items:
            problems.append("в предложении нет ни одной позиции")
        if any(item.price < 0 for item in self.items):
            problems.append("отрицательная цена в одной из позиций")
        if self.discount_percent < 0 or self.discount_percent > 100:
            problems.append("скидка должна быть от 0 до 100%")
        return problems

    @property
    def filename(self) -> str:
        """Имя файла: `КП-12-ромашка.pdf` — латиницей, чтобы не ломалось."""
        parts = ["KP", _slug(self.number) or "1"]
        client = _slug(self.client.name)
        if client:
            parts.append(client)
        return "-".join(parts)[:80] + ".pdf"


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}


def _slug(text: str) -> str:
    out = []
    for char in text.lower():
        if char in _TRANSLIT:
            out.append(_TRANSLIT[char])
        elif char.isascii() and (char.isalnum()):
            out.append(char)
        elif char in " -_.":
            out.append("-")
    slug = "".join(out)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")


def _int(value: object, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default
