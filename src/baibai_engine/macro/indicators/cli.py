from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import assert_never

from baibai_engine.foundation.env import load_project_env

from .db import DEFAULT_DB_PATH, IndicatorsSchemaError
from .definitions import SeriesDefinition
from .providers import IndicatorsProviderError, provider_spec
from .service import (
    IndicatorsService,
    QueryResult,
    RefreshFailure,
    RefreshSuccess,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine macro")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="list registered macro indicator series")
    list_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    list_parser.add_argument("--category")
    list_parser.add_argument("--format", choices=("table", "json"), default="table")

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
    refresh_range = refresh_parser.add_mutually_exclusive_group(required=True)
    refresh_range.add_argument("--start", type=date.fromisoformat)
    refresh_range.add_argument("--all-history", action="store_true")
    refresh_parser.add_argument("--end", required=True, type=date.fromisoformat)

    retract_parser = subparsers.add_parser(
        "retract",
        help="withdraw stored observation dates from every read",
    )
    retract_parser.add_argument("series_id")
    retract_parser.add_argument(
        "--observed-at",
        required=True,
        action="append",
        dest="observed_at",
        type=date.fromisoformat,
        help="observation date to withdraw (repeatable)",
    )
    retract_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    return parser


def main(argv: list[str] | None = None) -> int:
    load_project_env()
    args = build_parser().parse_args(argv)
    service = IndicatorsService(args.db)
    try:
        match args.command:
            case "list":
                series = service.list_series(category=args.category)
                if args.format == "json":
                    _print_series_json(series)
                else:
                    _print_series(series)
                return 0
            case "search":
                _print_series(service.search(args.query))
                return 0
            case "get":
                result = _run_get(service, args)
                _print_observations(result)
                return 0
            case "refresh":
                return _run_refresh(service, args)
            case "retract":
                return _run_retract(service, args)
    except KeyError as exc:
        message = exc.args[0] if exc.args else str(exc)
        print(f"error: {message}", file=sys.stderr)
        return 1
    except IndicatorsSchemaError as exc:
        print(
            f"error: {exc}; remove the local indicators cache and retry if it is disposable",
            file=sys.stderr,
        )
        return 1
    except (sqlite3.Error, OSError) as exc:
        print(f"error: unable to open indicators db: {args.db}: {exc}", file=sys.stderr)
        return 1
    except (ValueError, IndicatorsProviderError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    raise AssertionError(f"unreachable command: {args.command!r}")


def _run_refresh(service: IndicatorsService, args: argparse.Namespace) -> int:
    """Refresh every requested series, then report the failures as a set.

    One failing source must not leave the rest of the requested series stale, so
    each series is refreshed independently and the exit code reflects whether any
    failed. The detail lines are followed by a single-line roll-up of the failed
    series IDs: a batch log reader keeps only the tail of stderr (the daily batch
    summarizes the last 20 lines), so with many failures that one line is what
    survives.
    """

    outcomes = service.refresh_series(
        args.series_ids,
        start=None if args.all_history else args.start,
        end=args.end,
    )
    failures: list[RefreshFailure] = []
    for outcome in outcomes:
        match outcome:
            case RefreshSuccess(result=result):
                if args.all_history:
                    _print_refresh_summary(result)
                else:
                    _print_observations(result)
            case RefreshFailure():
                failures.append(outcome)
            case _:  # pragma: no cover - exhaustiveness guard over RefreshOutcome
                assert_never(outcome)
    if not failures:
        return 0
    print(
        f"error: {len(failures)} of {len(outcomes)} series failed to refresh:",
        file=sys.stderr,
    )
    for failure in failures:
        print(f"- {failure.series_id}: {failure.message}", file=sys.stderr)
    print(
        f"error: refresh failed for {len(failures)} of {len(outcomes)} series: "
        f"{', '.join(failure.series_id for failure in failures)}",
        file=sys.stderr,
    )
    return 1


def _run_retract(service: IndicatorsService, args: argparse.Namespace) -> int:
    """Print what each date lost and what it fell back to, so the change reads at a glance."""

    requested = sorted(set(args.observed_at))
    outcomes = service.retract(args.series_id, requested)
    handled = {item.observed_at for item in outcomes}
    restored = sum(1 for item in outcomes if item.restored is not None)
    print("series_id\tobserved_at\toutcome\twithdrawn_value\trestored_value")
    for item in outcomes:
        outcome = "withdrawn" if item.restored is None else "restored-previous"
        restored_value = "-" if item.restored is None else f"{item.restored.value:g}"
        print(
            f"{item.series_id}\t{item.observed_at.isoformat()}\t{outcome}\t"
            f"{item.withdrawn.value:g}\t{restored_value}"
        )
    for observed_at in requested:
        if observed_at not in handled:
            print(f"{args.series_id}\t{observed_at.isoformat()}\talready-retracted\t-\t-")
    print(
        f"retracted {len(outcomes)} of {len(requested)} requested observation dates; "
        f"{restored} fell back to an earlier vintage, {len(outcomes) - restored} left the reads"
    )
    return 0


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
    # Structured contract for machine consumers (the daily batch), so callers
    # parse fields instead of splitting the human table output.
    payload = [
        {
            "series_id": item.series_id,
            "name": item.name,
            "category": item.category,
            "geography": item.geography,
            "frequency": item.frequency,
            "unit": item.unit,
            "provider": item.provider,
            # "http" fetches externally; "local" is a derived series the batch
            # must refresh after its base inputs.
            "kind": provider_spec(item.provider).kind,
        }
        for item in series
    ]
    print(json.dumps(payload, ensure_ascii=False))


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


def _print_refresh_summary(result: QueryResult) -> None:
    first = result.observations[0].observed_at.isoformat() if result.observations else "-"
    last = result.observations[-1].observed_at.isoformat() if result.observations else "-"
    print(
        f"{result.series.series_id}\t{result.series.provider}\t"
        f"{len(result.observations)}\t{first}\t{last}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
