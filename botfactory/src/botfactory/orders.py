"""Разбор заказа: «сделай бота для магазина, название Магазин у дома».

Заказчик пишет фразой, а генератору нужны имя пакета, название, режим
публикации и приватность. Разбор намеренно снисходительный: чего не поняли —
подставляем умолчание и показываем карточку с кнопками, где всё можно
поправить до сборки. Ошибку выдаём только если бота не из чего собрать.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from botkit.publish import MODE_BRANCH, MODE_REPO
from botkit.scaffold import ScaffoldError, check_package_name
from botkit.texts import translit

#: Слова-обёртки: «сделай бота для…». Смысла не несут, но мешают найти имя.
NOISE = {
    "сделай", "создай", "собери", "сгенерируй", "заведи", "запили", "построй",
    "новый", "новая", "новое", "нового", "бот", "бота", "телеграм", "телеграмм",
    "telegram", "мне", "нужен", "нужна", "нужно", "для", "про", "пожалуйста",
    "плиз", "и", "в", "с", "на", "по",
}  # fmt: skip

#: Типовые боты: по слову заказчика — имя пакета и название по умолчанию.
#: Так «сделай бота для записи» превращается в `booking`, а не в `zapisi`.
KINDS: dict[str, tuple[str, str]] = {
    "магазин": ("shop", "Магазин"),
    "товар": ("shop", "Магазин"),
    "запис": ("booking", "Запись на услуги"),
    "брон": ("booking", "Бронирование"),
    "лид": ("lead_magnet", "Лид-магнит"),
    "рассыл": ("mailing", "Рассылка"),
    "отзыв": ("reviews", "Отзывы"),
    "заявк": ("requests", "Заявки"),
    "достав": ("delivery", "Доставка"),
    "парсер": ("parser", "Парсер"),
    "опрос": ("survey", "Опрос"),
    "поддержк": ("support", "Поддержка"),
    "меню": ("menu", "Меню"),
    "курс": ("courses", "Курсы"),
}

_QUOTED = re.compile(r"[«\"'“]([^»\"'”]{2,80})[»\"'”]")
_NAMED = re.compile(r"(?:названи[ея]|назови|назвать|имя)\s*[:\-—]?\s*(.+)", re.IGNORECASE)
_REPO = re.compile(r"(?:репозитори[йи]|репо|repo)\s*[:\-—]?\s*([A-Za-z][\w.-]*)")
_LATIN = re.compile(r"^[a-z][a-z0-9_-]*$")
_WORD = re.compile(r"[^\w\s-]+", re.UNICODE)
#: Слово «бот» в любой форме — признак того, что это вообще заказ.
_INTENT = re.compile(r"бот\w*|\bbot\w*", re.IGNORECASE)

#: После «репозиторий» может идти не имя, а продолжение фразы.
_NOT_A_REPO_NAME = {"new", "novyy", "etot", "current"}


class OrderError(ValueError):
    """Из сообщения не собрать заказ — показываем пользователю как есть."""


@dataclass(frozen=True, slots=True)
class Order:
    """Что собирать и куда публиковать."""

    package: str
    title: str
    mode: str = MODE_REPO
    private: bool = True
    repo: str = ""

    def toggled_mode(self) -> Order:
        return replace(self, mode=MODE_BRANCH if self.mode == MODE_REPO else MODE_REPO)

    def toggled_private(self) -> Order:
        return replace(self, private=not self.private)


def parse(text: str, *, mode: str = MODE_REPO, private: bool = True) -> Order:
    """Разобрать сообщение. `mode` и `private` — умолчания из настроек."""
    raw = (text or "").strip()
    if not raw:
        raise OrderError("пустое сообщение")
    # Команда бота — такой же заказ: «/new магазин» и «сделай бота магазин».
    command, raw = bool(re.match(r"^/\w+", raw)), re.sub(r"^/\w+(@\w+)?\s*", "", raw)

    title = _title(raw)
    repo = _repo(raw)
    mode = _mode(raw, mode)
    private = _private(raw, private)

    rest = _strip_recognized(raw, title, repo)
    package, default_title = _package(rest, raw, intent=command or bool(_INTENT.search(raw)))

    return Order(
        package=package,
        title=title or default_title,
        mode=mode,
        private=private,
        repo=repo,
    )


def _title(raw: str) -> str:
    """Название бота: в кавычках или после слова «название»."""
    quoted = _QUOTED.search(raw)
    if quoted:
        return quoted.group(1).strip()
    named = _NAMED.search(raw)
    if named:
        # До запятой: «название Магазин у дома, публичный» — название только
        # до перечисления остальных пожеланий.
        return named.group(1).split(",")[0].strip(" .;")
    return ""


def _repo(raw: str) -> str:
    match = _REPO.search(raw)
    if not match:
        return ""
    name = match.group(1)
    return "" if name.lower() in _NOT_A_REPO_NAME else name


def _mode(raw: str, default: str) -> str:
    lowered = raw.lower()
    if any(word in lowered for word in ("веткой", "ветк", "сюда", "в этот", "в текущ")):
        return MODE_BRANCH
    if any(word in lowered for word in ("отдельн", "новый репозитор", "свой репозитор")):
        return MODE_REPO
    return default


def _private(raw: str, default: bool) -> bool:
    lowered = raw.lower()
    if "публичн" in lowered or "открыт" in lowered or "public" in lowered:
        return False
    if "приватн" in lowered or "закрыт" in lowered or "private" in lowered:
        return True
    return default


def _strip_recognized(raw: str, title: str, repo: str) -> list[str]:
    """Выкинуть распознанное и служебные слова — останется вид бота."""
    text = _QUOTED.sub(" ", raw)
    text = _NAMED.sub(" ", text)
    text = _REPO.sub(" ", text)
    if title:
        text = text.replace(title, " ")
    if repo:
        text = text.replace(repo, " ")
    text = _WORD.sub(" ", text.lower())
    return [word for word in text.split() if word and word not in NOISE]


def _package(words: list[str], raw: str, *, intent: bool) -> tuple[str, str]:
    """Имя пакета и название по умолчанию.

    Порядок важен: явное латинское имя сильнее словаря, словарь — сильнее
    транслитерации. Иначе «сделай бота shop для магазина» дал бы `magazina`.

    `intent` — было ли в сообщении слово «бот» или команда. Без него из
    любого слова получался бы заказ, и «привет» собирало бы бота `privet`.
    """
    if intent:
        for word in words:
            if _LATIN.match(word):
                try:
                    return check_package_name(word), word.replace("_", " ").capitalize()
                except ScaffoldError:
                    continue

    # Знакомый вид бота узнаём и без слова «бот»: «магазин» — это заказ.
    for word in words:
        for stem, (package, title) in KINDS.items():
            if word.startswith(stem):
                return package, title

    if intent:
        for word in words:
            candidate = _as_package(word)
            if candidate:
                return candidate, word.capitalize()

    raise OrderError(
        "не понял, какого бота собрать. Напишите, например: "
        "«сделай бота для магазина, название Магазин у дома» или «сделай бота shop»"
        + (f"\nИз «{raw.strip()}» имя пакета не получилось." if raw.strip() else "")
    )


def _as_package(word: str) -> str:
    """Русское слово в имя пакета: «магазин» → `magazin`."""
    candidate = re.sub(r"[^a-z0-9_]+", "_", translit(word)).strip("_")
    try:
        return check_package_name(candidate)
    except ScaffoldError:
        return ""
