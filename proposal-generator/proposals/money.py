"""Деньги: округление, форматирование и сумма прописью.

Всё считаем в `Decimal` и округляем по-бухгалтерски (ROUND_HALF_UP) — с
плавающей точкой скидка 12.5% от 1999 ₽ рано или поздно даёт «итого» на копейку
меньше суммы строк, и это замечают.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENTS = Decimal("0.01")

CURRENCIES: dict[str, dict[str, str]] = {
    "RUB": {"symbol": "₽", "one": "рубль", "few": "рубля", "many": "рублей",
            "sub_one": "копейка", "sub_few": "копейки", "sub_many": "копеек", "gender": "m"},
    "USD": {"symbol": "$", "one": "доллар", "few": "доллара", "many": "долларов",
            "sub_one": "цент", "sub_few": "цента", "sub_many": "центов", "gender": "m"},
    "EUR": {"symbol": "€", "one": "евро", "few": "евро", "many": "евро",
            "sub_one": "цент", "sub_few": "цента", "sub_many": "центов", "gender": "m"},
    "KZT": {"symbol": "₸", "one": "тенге", "few": "тенге", "many": "тенге",
            "sub_one": "тиын", "sub_few": "тиына", "sub_many": "тиынов", "gender": "m"},
    "BYN": {"symbol": "Br", "one": "рубль", "few": "рубля", "many": "рублей",
            "sub_one": "копейка", "sub_few": "копейки", "sub_many": "копеек", "gender": "m"},
    "UAH": {"symbol": "₴", "one": "гривна", "few": "гривны", "many": "гривен",
            "sub_one": "копейка", "sub_few": "копейки", "sub_many": "копеек", "gender": "f"},
}


def money(value: object) -> Decimal:
    """Что угодно из формы → Decimal. «1 234,50 ₽» тоже понимаем."""
    if isinstance(value, Decimal):
        return value.quantize(CENTS, rounding=ROUND_HALF_UP)
    if isinstance(value, (int, float)):
        return Decimal(str(value)).quantize(CENTS, rounding=ROUND_HALF_UP)
    text = str(value or "0").strip()
    if not text:
        return Decimal("0.00")
    cleaned = (
        text.replace(" ", "")
        .replace(" ", "")
        .replace(" ", "")
        .replace(",", ".")
    )
    cleaned = "".join(char for char in cleaned if char.isdigit() or char in ".-")
    if cleaned.count(".") > 1:  # «1.234.567» — это разделители разрядов
        head, _, tail = cleaned.rpartition(".")
        cleaned = head.replace(".", "") + "." + tail
    try:
        return Decimal(cleaned or "0").quantize(CENTS, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return Decimal("0.00")


def quantity(value: object) -> Decimal:
    """Количество: до трёх знаков, чтобы поместились часы вида 1,5 и 0,25."""
    if isinstance(value, Decimal):
        raw = value
    elif isinstance(value, (int, float)):
        raw = Decimal(str(value))
    else:
        text = str(value or "0").strip().replace(",", ".")
        text = "".join(char for char in text if char.isdigit() or char in ".-")
        try:
            raw = Decimal(text or "0")
        except InvalidOperation:
            raw = Decimal("0")
    return raw.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def format_amount(value: Decimal, currency: str = "RUB", *, with_symbol: bool = True) -> str:
    """1234.5 → «1 234,50 ₽» (неразрывные пробелы, чтобы не рвалось при вёрстке)."""
    value = money(value)
    sign = "−" if value < 0 else ""
    whole, _, fraction = f"{abs(value):.2f}".partition(".")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    text = f"{sign}{' '.join(groups)},{fraction}"
    if with_symbol:
        symbol = CURRENCIES.get(currency, {}).get("symbol", currency)
        text = f"{text} {symbol}"
    return text


def format_quantity(value: Decimal) -> str:
    """Количество без лишних нулей: 2, 1,5, 0,25."""
    text = f"{quantity(value):.3f}".rstrip("0").rstrip(".")
    return (text or "0").replace(".", ",")


def format_percent(value: Decimal) -> str:
    text = f"{Decimal(value):.2f}".rstrip("0").rstrip(".")
    return (text or "0").replace(".", ",")


# --- сумма прописью ------------------------------------------------------------

_ONES_M = ("", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять")
_ONES_F = ("", "одна", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять")
_TEENS = ("десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
          "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать")
_TENS = ("", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят",
         "восемьдесят", "девяносто")
_HUNDREDS = ("", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот",
             "восемьсот", "девятьсот")

_SCALES: tuple[tuple[str, str, str, str], ...] = (
    ("", "", "", "m"),
    ("тысяча", "тысячи", "тысяч", "f"),
    ("миллион", "миллиона", "миллионов", "m"),
    ("миллиард", "миллиарда", "миллиардов", "m"),
    ("триллион", "триллиона", "триллионов", "m"),
)


def plural(number: int, one: str, few: str, many: str) -> str:
    """Русское согласование: 1 рубль, 2 рубля, 5 рублей."""
    number = abs(number) % 100
    if 11 <= number <= 14:
        return many
    match number % 10:
        case 1:
            return one
        case 2 | 3 | 4:
            return few
        case _:
            return many


def _group_words(value: int, gender: str) -> list[str]:
    words: list[str] = []
    if value >= 100:
        words.append(_HUNDREDS[value // 100])
        value %= 100
    if 10 <= value <= 19:
        words.append(_TEENS[value - 10])
        return words
    if value >= 20:
        words.append(_TENS[value // 10])
        value %= 10
    if value:
        words.append((_ONES_F if gender == "f" else _ONES_M)[value])
    return words


def number_to_words(value: int, gender: str = "m") -> str:
    """Целое число прописью: 1024 → «одна тысяча двадцать четыре»."""
    if value == 0:
        return "ноль"
    prefix = "минус " if value < 0 else ""
    value = abs(value)

    groups: list[int] = []
    while value:
        groups.append(value % 1000)
        value //= 1000
    if len(groups) > len(_SCALES):
        raise ValueError("слишком большое число для записи прописью")

    words: list[str] = []
    for index in range(len(groups) - 1, -1, -1):
        group = groups[index]
        if not group:
            continue
        one, few, many, scale_gender = _SCALES[index]
        words += _group_words(group, scale_gender if index else gender)
        if one:
            words.append(plural(group, one, few, many))
    return prefix + " ".join(word for word in words if word)


def amount_in_words(value: Decimal, currency: str = "RUB") -> str:
    """«1 200,50 ₽» → «Одна тысяча двести рублей 50 копеек».

    Копейки принято писать цифрами — так меньше поводов для спора о сумме.
    """
    spec = CURRENCIES.get(currency)
    value = money(value)
    whole = int(abs(value))
    cents = int((abs(value) - whole) * 100)

    if spec is None:  # незнакомая валюта — пишем код как есть
        words = number_to_words(whole)
        text = f"{words} {currency} {cents:02d}"
    else:
        words = number_to_words(whole, spec["gender"])
        unit = plural(whole, spec["one"], spec["few"], spec["many"])
        sub = plural(cents, spec["sub_one"], spec["sub_few"], spec["sub_many"])
        text = f"{words} {unit} {cents:02d} {sub}"

    if value < 0:
        text = f"минус {text}"
    return text[0].upper() + text[1:]
