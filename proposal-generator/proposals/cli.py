"""Командная строка генератора коммерческих предложений."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import storage
from .models import Proposal
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
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="открыть веб-форму в браузере")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--data", default=str(DEFAULT_DIR), help="каталог с черновиками")

    render_cmd = sub.add_parser("render", help="собрать PDF из файла черновика")
    render_cmd.add_argument("source", help="путь к .proposal.json или «-» для чтения stdin")
    render_cmd.add_argument("--out", default=None, help="куда сохранить PDF")
    render_cmd.add_argument("--logo-client", default=None, help="PNG или JPEG логотипа заказчика")
    render_cmd.add_argument("--logo-sender", default=None, help="PNG или JPEG вашего логотипа")
    render_cmd.add_argument("--accent", default=None, help="акцентный цвет, например #1f5eff")
    render_cmd.add_argument("--template", default=None, choices=sorted(TEMPLATES))

    demo = sub.add_parser("demo", help="сгенерировать пример предложения")
    demo.add_argument("--out", default="demo.pdf")
    demo.add_argument("--json", default=None, help="заодно сохранить черновик в JSON")
    demo.add_argument("--template", default="classic", choices=sorted(TEMPLATES))
    demo.add_argument("--accent", default=None)

    drafts = sub.add_parser("drafts", help="показать сохранённые черновики")
    drafts.add_argument("--data", default=str(DEFAULT_DIR))

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
    return 1


def _serve(args: argparse.Namespace) -> int:
    from .server import serve

    serve(host=args.host, port=args.port, data_dir=args.data, font=args.font)
    return 0


def _render(args: argparse.Namespace) -> int:
    if args.source == "-":
        import json

        proposal = storage.from_json(json.loads(sys.stdin.read()))
    else:
        proposal = storage.load(args.source)

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
