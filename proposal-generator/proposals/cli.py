"""Командная строка генератора коммерческих предложений."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import invoice as invoice_module
from . import mailer, storage
from .delivery import summarize
from .mailer import MailError
from .models import Proposal
from .money import format_amount
from .pdf import FontError, find_family
from .sample import demo_proposal
from .storage import DEFAULT_DIR, Drafts, StorageError
from .template import TEMPLATES, render


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m proposals",
        description="Генератор коммерческих предложений: JSON или веб-форма → PDF с логотипом.",
    )
    parser.add_argument("--font", default=None,
                        help="путь к .ttf или название семейства (по умолчанию — что найдётся)")
    parser.add_argument("--data", default=str(DEFAULT_DIR), help="каталог с черновиками")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="открыть веб-форму в браузере")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--public-url", default="",
                       help="адрес, по которому заказчик откроет ссылку, например https://kp.студия.рф")
    serve.add_argument("--key", default="",
                       help="ключ доступа к форме (нужен, только если --host смотрит наружу)")

    render_cmd = sub.add_parser("render", help="собрать PDF из файла черновика")
    render_cmd.add_argument("source", help="путь к .proposal.json или «-» для чтения stdin")
    render_cmd.add_argument("--out", default=None, help="куда сохранить PDF")
    render_cmd.add_argument("--logo-client", default=None, help="PNG или JPEG логотипа заказчика")
    render_cmd.add_argument("--logo-sender", default=None, help="PNG или JPEG вашего логотипа")
    render_cmd.add_argument("--accent", default=None, help="акцентный цвет, например #1f5eff")
    render_cmd.add_argument("--template", default=None, choices=sorted(TEMPLATES))

    send = sub.add_parser("send", help="отправить сохранённый черновик заказчику письмом")
    send.add_argument("slug", help="имя черновика (см. «proposals status»)")
    send.add_argument("--to", default="", help="адрес получателя, по умолчанию — почта заказчика")
    send.add_argument("--subject", default="", help="тема письма")
    send.add_argument("--message", default="", help="текст письма вместо стандартного")
    send.add_argument("--public-url", default="",
                      help="адрес сервера, чтобы в письмо попала ссылка на предложение")
    send.add_argument("--dry-run", action="store_true",
                      help="показать письмо, ничего не отправляя")

    smtp = sub.add_parser("smtp", help="настройки почты")
    smtp.add_argument("--host", default=None)
    smtp.add_argument("--port", type=int, default=None)
    smtp.add_argument("--user", default=None)
    smtp.add_argument("--password", default=None,
                      help="пароль приложения; «-» — прочитать со стандартного ввода")
    smtp.add_argument("--sender", default=None, help="адрес в поле «От кого»")
    smtp.add_argument("--sender-name", default=None)
    smtp.add_argument("--ssl", default=None, choices=("ssl", "starttls", "plain"))
    smtp.add_argument("--check", action="store_true", help="проверить связь и вход")

    remind = sub.add_parser("remind", help="напомнить о предложениях, оставшихся без ответа")
    remind.add_argument("slug", nargs="?", default=None,
                        help="одно предложение; без него — все, кто молчит")
    remind.add_argument("--after", type=int, default=5,
                        help="сколько дней молчания считать поводом (по умолчанию 5)")
    remind.add_argument("--public-url", default="")
    remind.add_argument("--dry-run", action="store_true", help="показать письма, не отправляя")

    bill = sub.add_parser("invoice", help="счёт на оплату из черновика")
    bill.add_argument("source", help="имя черновика или путь к .proposal.json")
    bill.add_argument("--out", default=None)
    bill.add_argument("--number", default="", help="номер счёта, если он не равен номеру КП")
    bill.add_argument("--date", default="", help="дата счёта, по умолчанию — дата КП")

    status = sub.add_parser("status", help="воронка: что отправлено, просмотрено и принято")
    status.add_argument("slug", nargs="?", default=None, help="показать журнал одного предложения")

    demo = sub.add_parser("demo", help="сгенерировать пример предложения")
    demo.add_argument("--out", default="demo.pdf")
    demo.add_argument("--json", default=None, help="заодно сохранить черновик в JSON")
    demo.add_argument("--template", default="classic", choices=sorted(TEMPLATES))
    demo.add_argument("--accent", default=None)

    sub.add_parser("drafts", help="показать сохранённые черновики")
    sub.add_parser("font", help="какой шрифт будет использован")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        match args.command:
            case "serve":
                return _serve(args)
            case "render":
                return _render(args)
            case "send":
                return _send(args)
            case "remind":
                return _remind(args)
            case "invoice":
                return _invoice(args)
            case "smtp":
                return _smtp(args)
            case "status":
                return _status(args)
            case "demo":
                return _demo(args)
            case "drafts":
                return _drafts(args)
            case "font":
                return _font(args)
    except FontError as error:
        print(f"Шрифт: {error}", file=sys.stderr)
        return 2
    except StorageError as error:
        print(f"Черновик: {error}", file=sys.stderr)
        return 2
    except MailError as error:
        print(f"Почта: {error}", file=sys.stderr)
        return 2
    return 1


def _serve(args: argparse.Namespace) -> int:
    from .server import serve

    serve(
        host=args.host,
        port=args.port,
        data_dir=args.data,
        font=args.font,
        public_url=args.public_url,
        admin_token=args.key,
    )
    return 0


def _render(args: argparse.Namespace) -> int:
    if args.source == "-":
        record = storage.from_json(json.loads(sys.stdin.read()))
    else:
        record = storage.load(args.source)
    proposal = record.proposal

    if args.logo_client:
        proposal.client.logo = _read_logo(args.logo_client)
    if args.logo_sender:
        proposal.sender.logo = _read_logo(args.logo_sender)
    if args.accent:
        proposal.accent = args.accent
    if args.template:
        proposal.template = args.template

    out = Path(args.out) if args.out else Path(proposal.filename)
    return _write(proposal, out, args.font)


def _send(args: argparse.Namespace) -> int:
    drafts = Drafts(args.data)
    record = drafts.read(args.slug)
    config = mailer.load_config(args.data)

    for problem in record.proposal.validate():
        print(f"Предупреждение: {problem}", file=sys.stderr)

    data, warnings = render(record.proposal, font=args.font)
    for warning in warnings:
        print(f"Предупреждение: {warning}", file=sys.stderr)
    if not args.public_url:
        print(
            "Предупреждение: без --public-url в письме не будет ссылки, "
            "а значит не отметятся «просмотрено» и «принято».",
            file=sys.stderr,
        )

    message = mailer.deliver(
        config,
        record.proposal,
        record.delivery,
        data,
        to=args.to,
        base_url=args.public_url,
        subject=args.subject,
        body=args.message,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        print(f"Кому:  {message['To']}")
        print(f"Тема:  {message['Subject']}")
        print(f"Файл:  {record.proposal.filename} ({len(data) / 1024:.0f} КБ)")
        print("-" * 60)
        print(message.get_body(("plain",)).get_content().rstrip())
        print("-" * 60)
        print("Ничего не отправлено: это пробный прогон.")
        return 0

    drafts.write(args.slug, record)
    print(f"Отправлено на {record.delivery.recipient}")
    link = record.delivery.link(args.public_url) if args.public_url else ""
    if link:
        print(f"Ссылка для заказчика: {link}")
    return 0


def _remind(args: argparse.Namespace) -> int:
    drafts = Drafts(args.data)
    config = mailer.load_config(args.data)

    if args.slug:
        targets = [(args.slug, drafts.read(args.slug))]
    else:
        targets = []
        for info in drafts.list():
            record = storage.load(info.path)
            if record.delivery.needs_reminder(args.after):
                targets.append((info.slug, record))

    if not targets:
        print(f"Некому напоминать: никто не молчит дольше {args.after} дн.")
        return 0

    for slug, record in targets:
        days = record.delivery.silent_days()
        try:
            data, _ = render(record.proposal, font=args.font)
            message = mailer.remind(
                config,
                record.proposal,
                record.delivery,
                data,
                base_url=args.public_url,
                dry_run=args.dry_run,
            )
        except MailError as error:
            print(f"{slug}: {error}", file=sys.stderr)
            continue

        if args.dry_run:
            print(f"--- {slug} (молчит {days} дн.) → {message['To']} ---")
            print(message.get_body(("plain",)).get_content().rstrip())
            print()
            continue

        drafts.write(slug, record)
        print(f"{slug}: напоминание отправлено на {record.delivery.recipient} "
              f"(молчал {days} дн.)")
    if args.dry_run:
        print("Ничего не отправлено: это пробный прогон.")
    return 0


def _invoice(args: argparse.Namespace) -> int:
    # Счёт удобно выставлять и по имени черновика, и по файлу «на подумать».
    source = Path(args.source)
    record = storage.load(source) if source.exists() else Drafts(args.data).read(args.source)
    proposal = record.proposal

    issued = _parse_date(args.date) if args.date else None
    data, warnings = invoice_module.render(
        proposal, number=args.number, issued_at=issued, font=args.font
    )
    for warning in warnings:
        print(f"Предупреждение: {warning}", file=sys.stderr)

    number = args.number or proposal.number
    out = Path(args.out) if args.out else Path(proposal.filename.replace("KP-", "Schet-", 1))
    if str(out) == "-":
        sys.stdout.buffer.write(data)
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"Счёт № {number}: {out}  ({len(data) / 1024:.0f} КБ)")
    return 0


def _parse_date(text: str):
    from datetime import datetime

    for pattern in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text.strip(), pattern).date()
        except ValueError:
            continue
    raise StorageError(f"непонятная дата: {text!r} — ждём 2026-07-28 или 28.07.2026")


def _smtp(args: argparse.Namespace) -> int:
    config = mailer.load_config(args.data)
    changed = False

    if args.password == "-":
        args.password = sys.stdin.readline().strip()
    for field, value in (
        ("host", args.host),
        ("port", args.port),
        ("user", args.user),
        ("password", args.password),
        ("sender", args.sender),
        ("sender_name", args.sender_name),
        ("ssl_mode", args.ssl),
    ):
        if value is not None:
            setattr(config, field, value)
            changed = True

    if changed:
        path = mailer.save_config(config, args.data)
        print(f"Настройки: {path}")

    print(f"Сервер:     {config.host or '— не задан'}:{config.port} ({config.ssl_mode})")
    print(f"Логин:      {config.user or '—'}")
    print(f"От кого:    {config.formatted_sender() or '—'}")
    print(f"Пароль:     {'задан' if config.password else '— не задан'}")

    if args.check:
        print(mailer.check(config))
    elif not config.configured:
        print("\nЗаполните хотя бы хост и адрес отправителя, например:")
        print("  python3 -m proposals smtp --host smtp.yandex.ru --user me@yandex.ru \\")
        print("      --password - --ssl ssl --check")
    return 0


def _status(args: argparse.Namespace) -> int:
    drafts = Drafts(args.data)
    if args.slug:
        return _one_status(drafts, args.slug)

    found = drafts.list()
    if not found:
        print(f"В {drafts.directory} предложений нет.")
        return 0

    width = max(len(info.slug) for info in found)
    for info in found:
        label = f"[{info.status_label}]"
        silence = f"молчит {info.silent_days} дн." if info.silent_days else ""
        print(f"{info.slug:<{width}}  {label:<14} № {info.number:<6} "
              f"{info.total:>16}  {info.client or info.subject or '—'} {silence}".rstrip())

    items = []
    for info in found:
        try:
            record = storage.load(info.path)
        except StorageError:
            continue
        items.append((record.delivery.status, record.proposal.totals().total,
                      record.proposal.currency))
    summary = summarize(items)
    counts = summary["counts"]

    print()
    print(f"Всего {summary['total']}: черновиков {counts['draft']}, "
          f"отправлено {summary['delivered']}, просмотрено {counts['viewed']}, "
          f"принято {counts['accepted']}, отклонено {counts['declined']}")
    if summary["delivered"]:
        print(f"Конверсия: {summary['conversion']}%")
    from decimal import Decimal

    for currency, bucket in summary["money"].items():
        accepted = format_amount(Decimal(bucket["accepted"]), currency)
        pending = format_amount(Decimal(bucket["open"]), currency)
        print(f"Принято на {accepted}, ждёт ответа на {pending}")
    return 0


def _one_status(drafts: Drafts, slug: str) -> int:
    record = drafts.read(slug)
    proposal, state = record.proposal, record.delivery
    print(f"№ {proposal.number} — {proposal.subject or 'без темы'}")
    print(f"Заказчик:  {proposal.client.name or '—'}")
    print(f"Сумма:     {format_amount(proposal.totals().total, proposal.currency)}")
    print(f"Статус:    {state.label}")
    if state.recipient:
        print(f"Отправлено: {state.recipient}")
    if state.comment:
        print(f"Комментарий заказчика: {state.comment}")
    if state.events:
        print("\nЖурнал:")
        for event in state.events:
            when = event.at.astimezone().strftime("%d.%m.%Y %H:%M")
            detail = f" — {event.detail}" if event.detail else ""
            print(f"  {when}  {event.label}{detail}")
    return 0


def _demo(args: argparse.Namespace) -> int:
    proposal = demo_proposal()
    proposal.template = args.template
    if args.accent:
        proposal.accent = args.accent
    if args.json:
        saved = storage.save(proposal, args.json)
        print(f"Черновик: {saved}")
    return _write(proposal, Path(args.out), args.font)


def _write(proposal: Proposal, out: Path, font: str | None) -> int:
    for problem in proposal.validate():
        print(f"Предупреждение: {problem}", file=sys.stderr)

    data, warnings = render(proposal, font=font)
    for warning in warnings:
        print(f"Предупреждение: {warning}", file=sys.stderr)

    if str(out) == "-":
        sys.stdout.buffer.write(data)
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"PDF: {out}  ({len(data) / 1024:.0f} КБ)")
    return 0


def _drafts(args: argparse.Namespace) -> int:
    found = Drafts(args.data).list()
    if not found:
        print(f"В {args.data} черновиков нет.")
        return 0
    width = max(len(info.slug) for info in found)
    for info in found:
        updated = info.updated_at.strftime("%d.%m.%Y %H:%M")
        subject = info.subject or info.client or "без темы"
        print(f"{info.slug:<{width}}  № {info.number:<6} {updated}  {subject}")
    return 0


def _font(args: argparse.Namespace) -> int:
    family = find_family(args.font)
    print(f"Семейство: {family.name}")
    for style in ("regular", "bold", "italic", "bolditalic"):
        path = getattr(family, style)
        print(f"  {style:<11} {path if path else '— (подставим обычное начертание)'}")
    return 0


def _read_logo(path: str) -> bytes:
    file = Path(path).expanduser()
    try:
        return file.read_bytes()
    except OSError as error:
        raise StorageError(f"не читается логотип {file}: {error}") from error
