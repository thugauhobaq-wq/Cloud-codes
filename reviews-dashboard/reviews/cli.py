"""Командная строка парсера отзывов."""

from __future__ import annotations

import argparse
import json
import sys

from .analytics import summarize
from .httpclient import FetchError
from .pipeline import collect
from .sentiment import analyze, label_ru
from .sources import SOURCES, describe_sources
from .storage import DEFAULT_DB, Storage


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m reviews",
        description="Парсер отзывов о компании с дашбордом (Яндекс.Карты, 2ГИС, маркетплейсы).",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="путь к файлу базы SQLite")
    sub = parser.add_subparsers(dest="command", required=True)

    collect_cmd = sub.add_parser("collect", help="собрать отзывы источником")
    collect_cmd.add_argument("--source", required=True, choices=sorted(SOURCES))
    collect_cmd.add_argument("--query", default="", help="ссылка, id, артикул или путь к файлу")
    collect_cmd.add_argument("--company", default=None, help="под каким именем сохранить в базе")
    collect_cmd.add_argument("--limit", type=int, default=100)
    collect_cmd.add_argument("--from-file", default=None,
                             help="разобрать сохранённый ответ площадки вместо запроса в сеть")
    collect_cmd.add_argument("--api-key", default=None, help="ключ API (для 2ГИС)")

    sub.add_parser("sources", help="список источников")

    list_cmd = sub.add_parser("companies", help="что уже есть в базе")
    list_cmd.add_argument("--json", action="store_true")

    report = sub.add_parser("report", help="сводка по компании в консоли")
    report.add_argument("--company", default=None)
    report.add_argument("--source", default=None)
    report.add_argument("--json", action="store_true")

    export = sub.add_parser("export", help="выгрузить отзывы в JSON")
    export.add_argument("--company", default=None)
    export.add_argument("--out", default="-")

    check = sub.add_parser("sentiment", help="проверить тональность произвольного текста")
    check.add_argument("text")
    check.add_argument("--rating", type=float, default=None)

    serve = sub.add_parser("serve", help="запустить веб-дашборд")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "sources":
        for item in describe_sources():
            key = " (нужен ключ)" if item["needs_key"] else ""
            print(f"{item['name']:<12} {item['title']}{key}\n{' ' * 13}{item['query_hint']}")
        return 0

    if args.command == "sentiment":
        result = analyze(args.text, args.rating)
        print(f"{label_ru(result.label)}  score={result.score}  "
              f"уверенность={result.confidence}  слов найдено: {result.hits}")
        if result.aspects:
            print("аспекты:", ", ".join(f"{k}: {v:+.2f}" for k, v in result.aspects.items()))
        if result.keywords:
            print("ключевые слова:", ", ".join(result.keywords))
        return 0

    if args.command == "serve":
        from .server import run_server

        run_server(host=args.host, port=args.port, db_path=args.db)
        return 0

    with Storage(args.db) as storage:
        if args.command == "collect":
            options = {}
            if args.api_key:
                options["api_key"] = args.api_key
            try:
                stats = collect(
                    args.source, args.query, limit=args.limit, company=args.company,
                    storage=storage, from_file=args.from_file, **options,
                )
            except (FetchError, KeyError, OSError) as error:
                print(f"Ошибка: {error}", file=sys.stderr)
                return 1
            print(f"Источник {stats['source']}: получено {stats['fetched']}, "
                  f"добавлено {stats['added']}, дубликатов {stats['duplicates']}. "
                  f"Компания: {stats['company']}")
            return 0

        if args.command == "companies":
            companies = storage.companies()
            if args.json:
                print(json.dumps(companies, ensure_ascii=False, indent=2))
            elif not companies:
                print("База пуста. Начните с: python -m reviews collect --source demo "
                      "--query «Моя компания» --limit 150")
            else:
                for item in companies:
                    rating = f"{item['avg_rating']:.2f}" if item["avg_rating"] else "—"
                    print(f"{item['company']:<32} отзывов: {item['total']:<5} "
                          f"средняя оценка: {rating:<5} последний: {item['last_date'] or '—'}")
            return 0

        if args.command == "report":
            rows = storage.fetch(company=args.company, source=args.source)
            data = summarize(rows)
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
                return 0
            _print_report(data, args.company)
            return 0

        if args.command == "export":
            rows = storage.fetch(company=args.company)
            payload = json.dumps(rows, ensure_ascii=False, indent=2)
            if args.out == "-":
                print(payload)
            else:
                with open(args.out, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                print(f"Записано {len(rows)} отзывов в {args.out}")
            return 0

    return 0


def _print_report(data: dict, company: str | None) -> None:
    if not data["total"]:
        print("Нет данных. Соберите отзывы командой collect.")
        return
    title = company or "все компании"
    rating = f"{data['avg_rating']:.2f}" if data["avg_rating"] else "—"
    print(f"\n=== {title} ===")
    print(f"Отзывов: {data['total']}   средняя оценка: {rating}   "
          f"индекс тональности: {data['avg_score']:+.2f}")
    share = data["sentiment_share"]
    print(f"Позитив {share['positive']:.0%} / нейтрально {share['neutral']:.0%} / "
          f"негатив {share['negative']:.0%}")
    print(f"Период: {data['period']['from'] or '—'} — {data['period']['to'] or '—'}, "
          f"ответы компании: {data['reply_rate']:.0%}")
    print(f"Тренд: {data['trend']['status']} "
          f"(негатив {data['trend']['previous_negative_share']:.0%} → "
          f"{data['trend']['recent_negative_share']:.0%})")

    print("\nПо источникам:")
    for source in data["sources"]:
        avg = f"{source['avg_rating']:.2f}" if source["avg_rating"] else "—"
        print(f"  {source['source']:<14} {source['total']:<5} оценка {avg:<5} "
              f"тональность {source['avg_score']:+.2f}")

    if data["aspects"]:
        print("\nАспекты:")
        for aspect in data["aspects"][:8]:
            mark = "👍" if aspect["score"] > 0.05 else "👎" if aspect["score"] < -0.05 else "•"
            print(f"  {mark} {aspect['aspect']:<14} {aspect['score']:+.2f} "
                  f"({aspect['mentions']} упоминаний)")

    negative = data["keywords"]["negative"][:8]
    if negative:
        print("\nЧастые слова в негативе: " + ", ".join(f"{k['word']}×{k['count']}" for k in negative))


if __name__ == "__main__":
    raise SystemExit(main())
