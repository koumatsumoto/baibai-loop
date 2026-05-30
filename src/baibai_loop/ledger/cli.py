from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from baibai_loop._env import load_project_env
from baibai_loop.screening.config import DEFAULT_CACHE_DIR, DEFAULT_SQLITE_CACHE_DIR
from baibai_loop.screening.providers.jquants import (
    JQuantsDailyBar,
    JQuantsMarketCalendarDay,
    JQuantsProvider,
    JQuantsProviderError,
)
from baibai_loop.screening.sqlite_reader import read_daily_bars, read_market_calendar

from .market_data import PriceObservation, load_fallback_price_observations
from .retro import build_monthly_retro, write_monthly_retro
from .sync import sync_ledger


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-loop-ledger")
    subparsers = parser.add_subparsers(dest="command", required=True)
    sync_parser = subparsers.add_parser(
        "sync", help="sync investment memo decisions into the decision register"
    )
    sync_parser.add_argument("--root", type=Path, default=Path.cwd())
    sync_parser.add_argument("--dry-run", action="store_true")
    sync_parser.add_argument(
        "--require-market-data",
        action="store_true",
        help="fail when neither J-Quants data nor fallback observations can be loaded",
    )
    retro_parser = subparsers.add_parser(
        "retro",
        help="generate a monthly retro draft from ledger JSONL and optional reviews",
    )
    retro_parser.add_argument("--root", type=Path, default=Path.cwd())
    retro_parser.add_argument("--month", required=True, help="target month (YYYY-MM)")
    retro_parser.add_argument("--dry-run", action="store_true", help="print draft to stdout")
    retro_parser.add_argument("--overwrite", action="store_true", help="replace existing draft")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_project_env(args.root)
    if args.command == "sync":
        calendar, bars, fallback_observations, market_warnings = _load_market_data(
            args.root, os.environ
        )
        usable_market_data = bool((calendar and bars) or fallback_observations)
        if args.require_market_data and not usable_market_data:
            if market_warnings:
                for warning in market_warnings:
                    print(f"error: {warning}", file=sys.stderr)
            else:
                # research packet が無い等で warning も出ないケースを silent にしない。
                print(
                    "error: --require-market-data set but no market data could be loaded",
                    file=sys.stderr,
                )
            return 1
        result = sync_ledger(
            args.root,
            dry_run=args.dry_run,
            calendar=calendar,
            bars=bars,
            fallback_observations=fallback_observations,
        )
        for warning in market_warnings:
            print(f"warning: {warning}", file=sys.stderr)
        for warning in result.warnings:
            print(f"warning: {warning}", file=sys.stderr)
        if args.dry_run:
            for line in result.diff_lines:
                print(line)
        print(f"decisions={result.decision_count}" + (" dry_run=true" if args.dry_run else ""))
        return 0
    if args.command == "retro":
        try:
            if args.dry_run:
                draft = build_monthly_retro(args.root, args.month)
                print(draft.content, end="")
            else:
                draft = write_monthly_retro(
                    args.root,
                    args.month,
                    overwrite=args.overwrite,
                )
                print(f"wrote {draft.path}")
        except (FileExistsError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        for warning in draft.warnings:
            print(f"warning: {warning}", file=sys.stderr)
        return 0
    raise AssertionError(f"unreachable command: {args.command!r}")


def _load_market_data(
    root: Path,
    env: Mapping[str, str],
) -> tuple[
    tuple[date, ...],
    tuple[JQuantsDailyBar, ...],
    tuple[PriceObservation, ...],
    tuple[str, ...],
]:
    decision_dates = _discover_decision_dates(root)
    if not decision_dates:
        fallback_observations, fallback_warnings = load_fallback_price_observations(root)
        return (), (), fallback_observations, fallback_warnings
    start = min(decision_dates) - timedelta(days=10)
    end = max(datetime.now(UTC).date(), max(decision_dates))
    sqlite_cache_dir = root / DEFAULT_SQLITE_CACHE_DIR
    sqlite_path = sqlite_cache_dir / "market.sqlite"
    calendar_days = read_market_calendar(sqlite_path, min(decision_dates), end)
    bars = read_daily_bars(sqlite_path, start, end)
    warnings: list[str] = []
    token = env.get("JQUANTS_REFRESH_TOKEN")
    if (calendar_days is None or bars is None) and token:
        # cache_dir / sqlite_cache_dir は固定の相対 path (env override 廃止)。
        # 詳細は screening/config.py の同名コメント参照。`root` 配下に解決する
        # ことで、test 等で workspace を切り替えるユースケースにも対応する。
        cache_dir = root / DEFAULT_CACHE_DIR
        provider = JQuantsProvider(
            token,
            cache_dir,
            sqlite_path=sqlite_path,
        )
        try:
            if calendar_days is None:
                calendar_days = provider.get_mkt_calendar(min(decision_dates), end)
            if bars is None:
                bars = provider.get_eq_bars_daily_range(start, end)
        except JQuantsProviderError as exc:
            warnings.append(
                f"failed to load J-Quants market data; fallback observations may be used: {exc}"
            )
    elif not token and bars is None:
        warnings.append("JQUANTS_REFRESH_TOKEN is unset; fallback observations may be used")
    fallback_observations, fallback_warnings = load_fallback_price_observations(root)
    warnings.extend(fallback_warnings)
    calendar = _business_calendar(calendar_days)
    if not bars and not fallback_observations:
        warnings.append("no J-Quants bars or fallback price observations were loaded")
    elif bars and not calendar:
        warnings.append("J-Quants bars loaded but no business calendar was loaded")
    return calendar, tuple(bars or ()), fallback_observations, tuple(warnings)


def _business_calendar(
    calendar_days: list[JQuantsMarketCalendarDay] | None,
) -> tuple[date, ...]:
    if calendar_days is None:
        return ()
    return tuple(day.day for day in calendar_days if day.is_business_day)


def _discover_decision_dates(root: Path) -> tuple[date, ...]:
    dates: list[date] = []
    for path in sorted((root / "records/05-research").rglob("*.md")):
        try:
            dates.append(date.fromisoformat(path.name[:10]))
        except ValueError:
            continue
    return tuple(dates)


if __name__ == "__main__":
    raise SystemExit(main())
