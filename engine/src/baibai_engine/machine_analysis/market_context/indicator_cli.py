"""Read registered L2 indicator series from retained Collection inputs."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Iterable
from datetime import date
from pathlib import Path

from baibai_engine.data_collection.economic.store import DEFAULT_DB_PATH, EconomicSchemaError

from .indicator_service import IndicatorsService, QueryResult, list_series, search
from .indicators.definitions import SeriesDefinition


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine market-context indicators")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="list registered economic indicator series")
    list_parser.add_argument("--category")
    list_parser.add_argument("--format", choices=("table", "json"), default="table")

    search_parser = subparsers.add_parser("search", help="search registered series")
    search_parser.add_argument("query")

    get_parser = subparsers.add_parser("get", help="get observations for a series")
    get_parser.add_argument("series_id")
    get_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    get_parser.add_argument("--latest", action="store_true")
    get_parser.add_argument("--start", type=date.fromisoformat)
    get_parser.add_argument("--end", type=date.fromisoformat)

    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else list(argv)
    args = build_parser().parse_args(arguments)
    if args.command == "get":
        service = IndicatorsService(args.db)
    try:
        match args.command:
            case "list":
                series = list_series(category=args.category)
                if args.format == "json":
                    _print_series_json(series)
                else:
                    _print_series(series)
                return 0
            case "search":
                _print_series(search(args.query))
                return 0
            case "get":
                result = _run_get(service, args)
                _print_observations(result)
                return 0
    except KeyError as exc:
        message = exc.args[0] if exc.args else str(exc)
        print(f"error: {message}", file=sys.stderr)
        return 1
    except EconomicSchemaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (sqlite3.Error, OSError) as exc:
        print(f"error: indicators {args.command}: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    raise AssertionError(f"unreachable command: {args.command!r}")


def _run_get(service: IndicatorsService, args: argparse.Namespace) -> QueryResult:
    if args.latest:
        if args.start is not None or args.end is not None:
            raise ValueError("--latest cannot be combined with --start/--end")
        return service.get_latest(args.series_id)
    if args.start is None or args.end is None:
        raise ValueError("get requires either --latest or both --start and --end")
    return service.get_range(args.series_id, start=args.start, end=args.end)


def _print_series(series: Iterable[SeriesDefinition]) -> None:
    for item in series:
        print(
            f"{item.series_id}\t{item.name}\t{item.category}\t{item.geography}\t"
            f"{item.frequency}\t{item.unit}\t{item.provider}"
        )


def _print_series_json(series: Iterable[SeriesDefinition]) -> None:
    # L2 display contract; Collection lists external source acquisition targets.
    payload = [
        {
            "series_id": item.series_id,
            "name": item.name,
            "category": item.category,
            "geography": item.geography,
            "frequency": item.frequency,
            "unit": item.unit,
            "provider": item.provider,
        }
        for item in series
    ]
    print(json.dumps(payload, ensure_ascii=False))


def _print_observations(result: QueryResult) -> None:
    print("series_id\tobserved_at\tvalue\tunit\tprovider")
    for item in result.observations:
        print(
            f"{result.series.series_id}\t{item.observed_at.isoformat()}\t{item.value:g}\t"
            f"{item.unit}\t{result.series.provider}"
        )
        if item.notes:
            print(
                f"# {item.observed_at}: vintage_at={item.vintage_at}; "
                f"quality_flags={','.join(item.quality_flags)}"
            )
            for note in item.notes:
                print(f"# {note}")


if __name__ == "__main__":
    raise SystemExit(main())
