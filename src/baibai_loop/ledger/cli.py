from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from baibai_loop.screening.config import DEFAULT_CACHE_DIR
from baibai_loop.screening.providers.jquants import (
    JQuantsDailyBar,
    JQuantsProvider,
    JQuantsProviderError,
)

from .sync import sync_ledger


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-loop-ledger")
    subparsers = parser.add_subparsers(dest="command", required=True)
    sync_parser = subparsers.add_parser("sync", help="sync research decisions into JSONL ledgers")
    sync_parser.add_argument("--root", type=Path, default=Path.cwd())
    sync_parser.add_argument("--dry-run", action="store_true")
    sync_parser.add_argument(
        "--require-market-data",
        action="store_true",
        help="fail when J-Quants market data cannot be loaded",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "sync":
        calendar, bars, market_warnings = _load_market_data(args.root, os.environ)
        if args.require_market_data and (not calendar or not bars):
            for warning in market_warnings:
                print(f"error: {warning}", file=sys.stderr)
            return 1
        result = sync_ledger(args.root, dry_run=args.dry_run, calendar=calendar, bars=bars)
        for warning in market_warnings:
            print(f"warning: {warning}", file=sys.stderr)
        for warning in result.warnings:
            print(f"warning: {warning}", file=sys.stderr)
        if args.dry_run:
            for line in result.diff_lines:
                print(line)
        print(
            f"paper={result.paper_count} skipped={result.skipped_count}"
            + (" dry_run=true" if args.dry_run else "")
        )
        return 0
    raise AssertionError(f"unreachable command: {args.command!r}")


def _load_market_data(
    root: Path,
    env: Mapping[str, str],
) -> tuple[tuple[date, ...], tuple[JQuantsDailyBar, ...], tuple[str, ...]]:
    token = env.get("JQUANTS_REFRESH_TOKEN")
    if not token:
        return (), (), ("JQUANTS_REFRESH_TOKEN is unset; tracking prices remain null",)
    decision_dates = _discover_decision_dates(root)
    if not decision_dates:
        return (), (), ()
    start = min(decision_dates) - timedelta(days=10)
    end = max(datetime.now(UTC).date(), max(decision_dates))
    cache_dir = Path(env.get("SCREENING_CACHE_DIR", str(DEFAULT_CACHE_DIR)))
    provider = JQuantsProvider(token, cache_dir)
    try:
        calendar_days = provider.get_mkt_calendar(min(decision_dates), end)
        bars = provider.get_eq_bars_daily_range(start, end)
    except JQuantsProviderError as exc:
        return (), (), (f"failed to load J-Quants market data; tracking prices remain null: {exc}",)
    calendar = tuple(day.day for day in calendar_days if day.is_business_day)
    return calendar, tuple(bars), ()


def _discover_decision_dates(root: Path) -> tuple[date, ...]:
    dates: list[date] = []
    for path in sorted((root / "research").rglob("*.md")):
        try:
            dates.append(date.fromisoformat(path.name[:10]))
        except ValueError:
            continue
    return tuple(dates)


if __name__ == "__main__":
    raise SystemExit(main())
