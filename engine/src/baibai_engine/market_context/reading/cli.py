"""``baibai-engine market-context reading`` — print the L2 reading for an as-of date."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import cast

from baibai_engine.market_context.indicators import db as indicators_db
from baibai_engine.market_context.indicators.db import DEFAULT_DB_PATH, IndicatorsSchemaError
from baibai_engine.market_context.reading.query import read_store_snapshot, store_snapshot_payload

from .models import ReadingSnapshot, SeriesReading
from .rules import DEFAULT_RULES_PATH, ReadingRulesError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine market-context reading")
    parser.add_argument("--asof", required=True, type=date.fromisoformat)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES_PATH)
    parser.add_argument("--format", choices=("table", "json"), default="table")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        conn = indicators_db.open_read_only_connection(args.db)
        try:
            snapshot = read_store_snapshot(conn, asof=args.asof, rules_path=args.rules)
        finally:
            conn.close()
    except IndicatorsSchemaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (ReadingRulesError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (sqlite3.Error, OSError) as exc:
        print(f"error: unable to read indicators db: {args.db}: {exc}", file=sys.stderr)
        return 1
    payload = store_snapshot_payload(snapshot)
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(cast(dict[str, object], payload["read_semantics"])["limitation"])
        _print_table(snapshot)
    return 0


def _print_table(snapshot: ReadingSnapshot) -> None:
    print(f"# Reading asof={snapshot.asof.isoformat()} rules={snapshot.rules_revision}")
    print(
        "series_id\tlatest\tobserved_at\tnext_print\tdue_days\tstale_days\t3m\t12m\t"
        "statistic\tstat_value\tpercentile\tz\twindow\tnotes",
    )
    for reading in snapshot.series:
        notes = list(reading.flags)
        if reading.stale:
            notes.append("stale")
        if reading.insufficient_history:
            notes.append("insufficient_history")
        print(
            "\t".join(
                (
                    reading.series_id,
                    _number(reading.latest_value),
                    "-" if reading.observed_at is None else reading.observed_at.isoformat(),
                    (
                        "-"
                        if reading.next_print_estimate is None
                        else reading.next_print_estimate.isoformat()
                    ),
                    ("-" if reading.print_due_in_days is None else str(reading.print_due_in_days)),
                    "-" if reading.staleness_days is None else str(reading.staleness_days),
                    _direction_cell(reading, "short_trend"),
                    _direction_cell(reading, "long_trend"),
                    reading.statistic,
                    _number(reading.statistic_value),
                    _number(reading.percentile, digits=2),
                    _number(reading.z_score, digits=2),
                    f"{reading.window_years}y/{reading.window_observations}",
                    ",".join(notes) if notes else "-",
                )
            )
        )


def _direction_cell(reading: SeriesReading, attribute: str) -> str:
    trend = getattr(reading, attribute)
    if trend is None:
        return "-"
    return f"{trend.direction}({trend.change:+g})"


def _number(value: float | None, *, digits: int = 4) -> str:
    if value is None:
        return "-"
    return f"{value:.{digits}g}"


__all__ = ["build_parser", "main"]
