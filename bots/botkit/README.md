# botkit — каркас для Telegram-ботов

Общая часть, которая в каждом боте писалась заново: настройки, SQLite с
транзакциями, уведомления, рассылка, фоновые воркеры, корректная остановка по
`SIGTERM`. Плюс генератор: одна команда — и новый бот уже работает, с тестами,
Docker и CI.

```bash
pip install -e .
python -m botkit new shop --title "Магазин у дома"
cd shop-bot && pip install -e ".[dev]" && pytest -q
```

Или сразу в GitHub — сгенерировать, закоммитить и запушить одной командой:

```bash
GITHUB_TOKEN=ghp_… python -m botkit new shop --title "Магазин у дома" --push repo
```

Сгенерированный бот — не пустая заготовка, а работающий приём заявок: меню,
форма с телефоном, уведомление владельцу с кнопками «взять в работу» и
«закрыть», админка со статистикой и рассылкой, напоминание о заявках без
ответа. Дальше его правят под задачу.

## Зачем

Три бота из этого репозитория — [запись на услуги](../booking-bot/),
[магазин](../shop-bot/) и [лид-магнит](../leadmagnet-bot/) — совпадали
примерно на треть. Причём совпадали неточно: где-то `lower()` не знал
кириллицы, где-то ответ шёл через `callback.message.answer` и падал на
сообщении старше 48 часов, где-то проверка и запись шли двумя запросами
вместо транзакции. Такие места чинятся один раз здесь, а не в каждом боте по
отдельности.

## Что внутри

| Модуль | Что даёт |
| --- | --- |
| `config.BotSettings` | токен, владелец, `ADMIN_IDS` через запятую, путь к базе; `require()` с понятным сообщением о незаполненном `.env` |
| `storage.BaseStorage` | открытие базы, WAL, блокировка на общее соединение, `transaction()`, key-value настройки, `rulower` для поиска по-русски |
| `notify.Notifier` | уведомления админам и клиентам; недоступный получатель не роняет обработчик |
| `broadcast.Broadcaster` | рассылка с темпом ниже лимита Telegram, разбором блокировок и отчётом о прогрессе |
| `worker.PeriodicWorker` | фоновый цикл, переживающий исключения в `tick()` |
| `runner.run_bot` | сборка бота и воркеров, сигналы, отмена задач, закрытие соединений |
| `github.GitHub` | GitHub API на стандартной библиотеке: создать репозиторий, найти существующий |
| `publish.publish` | `git init`, коммит и пуш — отдельным репозиторием или веткой в текущем |
| `messaging` | `payload()`, безопасное редактирование сообщений, разбивка длинного текста по лимиту |
| `texts` | экранирование HTML, русская плюрализация, деньги, длительность, телефоны |
| `filters` | `IsAdmin`, `IsPrivate` |
| `testing` | `FakeBot` и `FakeSender` — проверять бота без Telegram |

## Как этим пользоваться

Настройки: наследуемся и добавляем своё.

```python
from botkit import BotSettings

class Settings(BotSettings):
    shop_name: str = "Магазин"
    delivery_price: int = 300

settings = Settings()          # читает .env
settings.require()             # BOT_TOKEN и OWNER_ID заполнены?
settings.admins()              # {владелец, ...ADMIN_IDS}
```

Хранилище: описываем схему, остальное готово.

```python
from botkit import BaseStorage

class Storage(BaseStorage):
    schema = """
    CREATE TABLE IF NOT EXISTS orders (
        id     INTEGER PRIMARY KEY AUTOINCREMENT,
        title  TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'new'
    );
    """

    async def add(self, title: str) -> int:
        return await self.execute("INSERT INTO orders (title) VALUES (?)", (title,))

    async def take(self, order_id: int) -> bool:
        # rowcount отличает «взял в работу» от повторного нажатия кнопки
        changed = await self.execute_changes(
            "UPDATE orders SET status = 'taken' WHERE id = ? AND status = 'new'",
            (order_id,),
        )
        return bool(changed)
```

Проверка и запись одним куском — там, где между ними нельзя никого пустить:

```python
async with storage.transaction() as conn:
    async with conn.execute("SELECT stock FROM products WHERE id = ?", (product_id,)) as cursor:
        row = await cursor.fetchone()
    if row["stock"] < qty:
        raise OutOfStock          # откат, ничего не записалось
    await conn.execute(
        "UPDATE products SET stock = stock - ? WHERE id = ?", (qty, product_id)
    )
```

Внутри `transaction()` работаем с выданным соединением: методы хранилища
берут ту же блокировку и здесь встанут намертво.

Запуск с фоновым воркером:

```python
from botkit import PeriodicWorker, make_bot, run_bot

class Reminders(PeriodicWorker):
    name = "напоминания"

    async def tick(self) -> int:
        ...  # вернуть, сколько отправлено

bot = make_bot(settings)
await run_bot(
    bot=bot,
    routers=[build_router(storage, notify, settings)],
    workers=[Reminders(interval=60)],
    commands=admin_commands(),
    storage=storage,
)
```

Рассылка без Telegram в тестах:

```python
from botkit import Broadcaster
from botkit.testing import FakeSender

sender = FakeSender(blocked={42})
progress = await Broadcaster(sender, on_blocked=storage.mark_blocked).run(
    [1, 42], "Скидка до пятницы"
)
assert progress.sent == 1 and progress.blocked == 1
```

## Генератор

```
python -m botkit new <пакет> [--dir .] [--project имя-каталога] [--title "Название"] [--force]
```

- `<пакет>` — имя импорта: `shop`, `booking`, `lead_magnet`. Дефис
  превращается в подчёркивание, непригодное имя отвергается сразу — ошибка
  здесь дешевле, чем `ModuleNotFoundError` после первой правки.
- каталог по умолчанию — `<пакет>-bot`.
- `--force` перезаписывает сгенерированный код, но чужие файлы в каталоге не
  трогает.

Что создаётся:

```
shop-bot/
  pyproject.toml, README.md, .env.example, .gitignore
  Dockerfile, docker-compose.yml, .github/workflows/ci.yml
  src/shop/
    config.py models.py storage.py texts.py keyboards.py notify.py reminders.py
    handlers/{client,admin}.py
  tests/  (16 тестов, проходят сразу)
```

Сгенерированный проект ставит botkit из исходников (`pip install -e ../botkit`) —
в PyPI он не опубликован. Это же учтено в его Dockerfile и workflow CI.

## Публикация в GitHub

Между «проект создан» и «код в GitHub» каждый раз одни и те же шаги. Они
делаются тем же вызовом:

```bash
export GITHUB_TOKEN=ghp_…                       # токен с правом на репозитории

python -m botkit new shop --push repo           # новый репозиторий и пуш в него
python -m botkit new shop --push branch         # ветка bot/shop-bot в текущем
python -m botkit publish shop-bot --push repo   # опубликовать готовый проект
```

| Ключ | Что делает |
| --- | --- |
| `--push repo` | создаёт репозиторий через GitHub API и пушит туда проект |
| `--push branch` | коммитит каталог веткой в репозиторий, внутри которого он лежит |
| `--repo`, `--owner` | имя репозитория и владелец (организация) |
| `--branch`, `--message` | имя ветки и текст коммита |
| `--public` | публичный репозиторий; по умолчанию приватный |
| `--token` | токен, если не хочется класть его в `GITHUB_TOKEN` |

То же из кода:

```python
from botkit import publish

result = publish(
    "shop-bot",
    mode="repo",
    token=token,
    name="shop-bot",
    private=False,
    description="Магазин у дома",
)
print(result.url)          # https://github.com/seller/shop-bot
```

Что здесь сделано не самым коротким способом и почему:

- **Токен не оседает в `.git/config`.** Он подставляется в адрес одного
  `git push` и вырезается из текста ошибок — иначе секрет уезжает в конфиг
  репозитория и в логи CI, откуда его потом никто не вычистит.
- **Занятое имя проверяется до генерации.** «Репозиторий уже есть, вот
  ссылка» полезнее, чем ошибка 422 после `git commit`.
- **Имя репозитория транслитерируется.** «Магазин у дома» →
  `magazin-u-doma`. GitHub заменил бы кириллицу сам, но молча — и ссылка
  вела бы не туда.
- **Ветка не забирает чужие изменения.** Если в репозитории есть
  незакоммиченные правки вне каталога проекта, публикация останавливается:
  `git checkout` утащил бы их в чужой коммит.
- **Повтор после сбоя на пуше доезжает.** Коммит уже сделан, нового ничего —
  пушим существующий, а не падаем на «нечего коммитить».

Поверх этого работает [`botfactory`](../botfactory/) — Telegram-бот, который
собирает ботов по фразе в чате.

## Тесты

```bash
pip install -e ".[dev]"
pytest -q
ruff check src tests
```

Отдельно проверяется сам генератор: сгенерированный проект компилируется и
запускается — шаблон должен давать рабочий Python, а не текст, похожий на код.

## Чего здесь нет

- Миграций схемы. Для ботов на SQLite хватает `CREATE TABLE IF NOT EXISTS` и
  `ALTER TABLE` в `on_open()`; тащить Alembic ради двух таблиц незачем.
- Webhook-режима. Все боты в репозитории работают на long polling — для
  нагрузки маленького бизнеса этого достаточно, а вебхук требует домена и
  сертификата.
- Оплаты. Она слишком разная у разных провайдеров, чтобы обобщать заранее.

Три существующих бота пока живут на своём коде: переписывать работающее и
покрытое тестами — риск без пользы. Новые боты собираются на botkit, старые
переводятся по мере правок.
