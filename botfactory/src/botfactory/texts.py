"""Формулировки бота. Собраны в одном месте — их правят чаще всего."""

from __future__ import annotations

from botkit.publish import MODE_REPO
from botkit.texts import counted, escape

from .factory import mode_title
from .orders import Order
from .storage import DONE, FAILED, STATUS_TITLES, Build

HELP = """<b>Фабрика ботов</b>

Напишите, какого бота собрать — я сгенерирую проект на botkit и запушу его в
GitHub. В ответ придёт ссылка.

Например:
• <code>сделай бота для магазина, название Магазин у дома</code>
• <code>сделай бота shop, публичный</code>
• <code>бот для записи, веткой в этот репозиторий</code>

Что можно указать словами: название в кавычках или после слова «название»,
«публичный» или «приватный», «отдельным репозиторием» или «веткой»,
«репозиторий имя-репозитория». Всё это правится кнопками перед сборкой.

Команды:
/builds — последние сборки
/help — эта справка"""

START = """Привет! Я собираю Telegram-ботов и кладу их в GitHub.

Напишите, например: <code>сделай бота для магазина, название Магазин у дома</code>.
Подробнее — /help"""

DENIED = "Этот бот собирает репозитории в чужом аккаунте, поэтому доступен только владельцу."


def card(order: Order, build_id: int) -> str:
    """Карточка заказа перед сборкой: что именно уедет в GitHub."""
    where = "новый репозиторий" if order.mode == MODE_REPO else "ветка в текущем репозитории"
    repo = order.repo or f"{order.package.replace('_', '-')}-bot"
    return (
        f"<b>Заказ №{build_id}</b>\n\n"
        f"Пакет: <code>{escape(order.package)}</code>\n"
        f"Название: {escape(order.title)}\n"
        f"Репозиторий: <code>{escape(repo)}</code>\n"
        f"Куда: {where}\n"
        f"Видимость: {'приватный' if order.private else 'публичный'}\n\n"
        "Проверьте и нажмите «Собрать» — или поправьте кнопками."
    )


def building(order: Order) -> str:
    return (
        f"Собираю <code>{escape(order.package)}</code> и пушу в GitHub…\n"
        "Это занимает несколько секунд."
    )


def done(build: Build, url: str, files: int) -> str:
    # files == 0 — повтор после сбоя на пуше: коммит был сделан раньше, а
    # доехал только сейчас. «В коммите 0 файлов» звучало бы как ошибка.
    what = (
        f"В коммите {counted(files, 'файл', 'файла', 'файлов')}."
        if files
        else "Новых файлов не было — доехал коммит с прошлого раза."
    )
    return (
        f"Готово: <b>{escape(build.title)}</b>\n\n"
        f"{url}\n\n"
        f"{what} Куда: {mode_title(build.mode)}.\n\n"
        "Дальше: склонируйте, впишите в <code>.env</code> токен от @BotFather и "
        "запустите <code>docker compose up -d --build</code>."
    )


def failed(error: str) -> str:
    return (
        f"Не получилось: {escape(error)}\n\n"
        "Поправьте заказ и нажмите «Повторить» — или напишите новый."
    )


def build_line(build: Build) -> str:
    when = build.created_at.strftime("%d.%m %H:%M") if build.created_at else ""
    status = STATUS_TITLES.get(build.status, build.status)
    line = f"№{build.id} · {when} · {escape(build.title)} · {status}"
    if build.status == DONE and build.url:
        line += f"\n  {build.url}"
    if build.status == FAILED and build.error:
        line += f"\n  {escape(build.error)}"
    return line


def builds(items: list[Build]) -> str:
    if not items:
        return "Сборок пока не было. Напишите, какого бота собрать — /help"
    return "<b>Последние сборки</b>\n\n" + "\n".join(build_line(item) for item in items)


def stats(counts: dict[str, int]) -> str:
    return (
        "<b>Сводка</b>\n\n"
        f"  всего заказов: {counts['total']}\n"
        f"  собрано:       {counts['done']}\n"
        f"  с ошибкой:     {counts['failed']}\n"
        f"  в работе:      {counts['waiting']}"
    )
