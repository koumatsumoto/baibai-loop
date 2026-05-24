from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Iterable
from datetime import date
from pathlib import Path

from .db import DEFAULT_DB_PATH, StatsSchemaError
from .definitions import SeriesDefinition
from .providers import StatsProviderError
from .service import QueryResult, StatsService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-loop-stats")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="list registered macro statistics series")
    list_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    list_parser.add_argument("--domain")

    search_parser = subparsers.add_parser("search", help="search registered series")
    search_parser.add_argument("query")
    search_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    get_parser = subparsers.add_parser("get", help="get observations for a series")
    get_parser.add_argument("series_id")
    get_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    get_parser.add_argument("--latest", action="store_true")
    get_parser.add_argument("--start", type=date.fromisoformat)
    get_parser.add_argument("--end", type=date.fromisoformat)

    refresh_parser = subparsers.add_parser("refresh", help="force provider refresh for series")
    refresh_parser.add_argument("series_ids", nargs="+")
    refresh_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    refresh_parser.add_argument("--start", required=True, type=date.fromisoformat)
    refresh_parser.add_argument("--end", required=True, type=date.fromisoformat)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    service = StatsService(args.db)
    try:
        match args.command:
            case "list":
                _print_series(service.list_series(domain=args.domain))
                return 0
            case "search":
                _print_series(service.search(args.query))
                return 0
            case "get":
                result = _run_get(service, args)
                _print_observations(result)
                return 0
            case "refresh":
                for series_id in args.series_ids:
                    result = service.get_range(
                        series_id,
                        start=args.start,
                        end=args.end,
                        refresh=True,
                    )
                    _print_observations(result)
                return 0
    except KeyError as exc:
        message = exc.args[0] if exc.args else str(exc)
        print(f"error: {message}", file=sys.stderr)
        return 1
    except StatsSchemaError as exc:
        print(
            f"error: {exc}; remove the local stats cache and retry if it is disposable",
            file=sys.stderr,
        )
        return 1
    except (sqlite3.Error, OSError) as exc:
        print(f"error: unable to open stats db: {args.db}: {exc}", file=sys.stderr)
        return 1
    except (ValueError, StatsProviderError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    raise AssertionError(f"unreachable command: {args.command!r}")


def _run_get(service: StatsService, args: argparse.Namespace) -> QueryResult:
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
            f"{item.series_id}\t{item.name}\t{item.domain}\t{item.geography}\t"
            f"{item.frequency}\t{item.unit}\t{item.provider}"
        )


def _print_observations(result: QueryResult) -> None:
    source = "cache" if result.cache_hit else "provider"
    print(
        "series_id\tobserved_at\tvalue\tunit\tprovider\tsource",
    )
    for item in result.observations:
        print(
            f"{result.series.series_id}\t{item.observed_at.isoformat()}\t{item.value:g}\t"
            f"{item.unit}\t{result.series.provider}\t{source}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
