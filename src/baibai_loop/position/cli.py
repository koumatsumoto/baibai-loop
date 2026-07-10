"""Command-line entry for position tracking (`baibai-loop-position`).

Computes open-position benchmark-relative return. The command needs J-Quants
daily bars to price holdings, so this entry point is the composition root that
loads market data from the screening SQLite cache and passes it into the
position-tracking logic; the position core modules themselves stay free of any
screening dependency.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import yaml

from baibai_loop.foundation.env import load_project_env
from baibai_loop.market.bars import JQuantsDailyBar, JQuantsMarketCalendarDay
from baibai_loop.market.config import DEFAULT_CACHE_DIR, DEFAULT_SQLITE_CACHE_DIR
from baibai_loop.market.provider import JQuantsMarketProvider, JQuantsProviderError
from baibai_loop.market.store import read_daily_bars, read_market_calendar
from baibai_loop.position.benchmark import (
    NIKKEI225_ETF_PROXY,
    PortfolioBenchmark,
    compute_forward_performance,
)
from baibai_loop.position.calibration import build_calibration_telemetry, telemetry_to_payload
from baibai_loop.position.trades import load_open_trades


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-loop-position")
    subparsers = parser.add_subparsers(dest="command", required=True)
    benchmark_parser = subparsers.add_parser(
        "benchmark",
        help="compute open-position return versus the Nikkei 225 ETF proxy",
    )
    benchmark_parser.add_argument("--root", type=Path, default=Path.cwd())
    benchmark_parser.add_argument("--asof", help="evaluation date (YYYY-MM-DD); defaults to today")
    benchmark_parser.add_argument(
        "--proxy",
        default=NIKKEI225_ETF_PROXY,
        help=f"benchmark ETF proxy ticker (default: {NIKKEI225_ETF_PROXY})",
    )
    benchmark_parser.add_argument(
        "--exclude-cohort-tags",
        default="",
        help=(
            "comma-separated cohort_tag values to exclude (e.g. "
            "'pre_refactor_backfill,user_position_confirmed'); empty includes everything"
        ),
    )
    calibration_parser = subparsers.add_parser(
        "calibration",
        description=(
            "Emit read-only YAML telemetry for monthly review_valuation / "
            "estimate_calibration drafts. Draft valuation_zone and action are "
            "mechanical review inputs, not automatic exit decisions."
        ),
        help=(
            "emit read-only YAML telemetry for monthly review_valuation / "
            "estimate_calibration drafts; draft actions are not automatic exit decisions"
        ),
    )
    calibration_parser.add_argument("--root", type=Path, default=Path.cwd())
    calibration_parser.add_argument(
        "--asof",
        help="evaluation date (YYYY-MM-DD); defaults to today",
    )
    calibration_parser.add_argument(
        "--proxy",
        default=NIKKEI225_ETF_PROXY,
        help=f"benchmark ETF proxy ticker (default: {NIKKEI225_ETF_PROXY})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_project_env(args.root)
    if args.command == "benchmark":
        excluded = tuple(tag.strip() for tag in args.exclude_cohort_tags.split(",") if tag.strip())
        return _run_benchmark(
            args.root,
            _resolve_asof(args.asof),
            args.proxy,
            os.environ,
            excluded_cohort_tags=excluded,
        )
    if args.command == "calibration":
        return _run_calibration(args.root, _resolve_asof(args.asof), args.proxy, os.environ)
    raise AssertionError(f"unreachable command: {args.command!r}")


def _resolve_asof(value: str | None) -> date:
    if value is None:
        return datetime.now(UTC).date()
    return date.fromisoformat(value)


def _run_benchmark(
    root: Path,
    asof: date,
    proxy: str,
    env: Mapping[str, str],
    *,
    excluded_cohort_tags: tuple[str, ...] = (),
) -> int:
    trades = load_open_trades(root)
    if excluded_cohort_tags:
        excluded_set = frozenset(excluded_cohort_tags)
        observed_tags = frozenset(t.cohort_tag for t in trades if t.cohort_tag is not None)
        # R4 P0 fix: surface typos. Silently dropping zero trades because the
        # cohort name does not match any observed tag was a real foot-gun —
        # the operator believes they're looking at a regulated cohort while
        # actually reading the unfiltered total. Emit a warning AND return 2
        # so CI / scripts can flag the bad invocation.
        unknown_tags = excluded_set - observed_tags
        if unknown_tags:
            print(
                f"warning: --exclude-cohort-tags has no match for "
                f"{sorted(unknown_tags)} (observed cohort_tag values: "
                f"{sorted(observed_tags) if observed_tags else 'none'})",
                file=sys.stderr,
            )
            return 2
        before = len(trades)
        trades = [trade for trade in trades if trade.cohort_tag not in excluded_set]
        excluded_count = before - len(trades)
        if excluded_count > 0:
            print(
                f"excluded {excluded_count} trade(s) with cohort_tag in {sorted(excluded_set)}",
                file=sys.stderr,
            )
    if not trades:
        print("no open positions")
        return 0
    _calendar, bars, market_warnings = _load_market_data(root, env)
    for warning in market_warnings:
        print(f"warning: {warning}", file=sys.stderr)
    result = compute_forward_performance(trades, asof, bars, proxy)
    for line in _format_benchmark(result):
        print(line)
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


def _run_calibration(root: Path, asof: date, proxy: str, env: Mapping[str, str]) -> int:
    trades = load_open_trades(root)
    _calendar, bars, market_warnings = _load_market_data(
        root,
        env,
        basis_dates=tuple(trade.entry_date for trade in trades),
        end=asof,
        allow_provider=False,
        require_calendar=False,
    )
    telemetry = build_calibration_telemetry(
        root,
        asof=asof,
        bars=bars,
        benchmark_ticker=proxy,
        market_warnings=market_warnings,
    )
    yaml.safe_dump(
        telemetry_to_payload(telemetry),
        sys.stdout,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    for warning in telemetry.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


def _format_benchmark(result: PortfolioBenchmark) -> list[str]:
    lines = [
        f"asof={result.asof.isoformat()} benchmark_proxy={result.benchmark_ticker} "
        f"positions={len(result.positions)}"
    ]
    for position in result.positions:
        lines.append(
            f"{position.ticker} {position.name} entry={position.entry_date.isoformat()} "
            f"qty={position.quantity} pnl={_fmt_yen(position.gross_pnl)} "
            f"ret={_fmt_pct(position.return_ratio)} bm={_fmt_pct(position.benchmark_return)} "
            f"rel={_fmt_pt(position.relative)}"
        )
    lines.append(
        f"TOTAL notional={result.total_notional:,.0f} pnl={_fmt_yen(result.total_gross_pnl)} "
        f"ret={_fmt_pct(result.portfolio_return)} bm={_fmt_pct(result.benchmark_return)} "
        f"rel={_fmt_pt(result.relative)}"
    )
    return lines


def _fmt_yen(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+,.0f}"


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:+.2f}%"


def _fmt_pt(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:+.2f}pt"


def _load_market_data(
    root: Path,
    env: Mapping[str, str],
    *,
    basis_dates: tuple[date, ...] | None = None,
    end: date | None = None,
    allow_provider: bool = True,
    require_calendar: bool = True,
) -> tuple[
    tuple[date, ...],
    tuple[JQuantsDailyBar, ...],
    tuple[str, ...],
]:
    decision_dates = basis_dates if basis_dates is not None else _discover_decision_dates(root)
    if not decision_dates:
        return (), (), ()
    start = min(decision_dates) - timedelta(days=10)
    end = end or max(datetime.now(UTC).date(), max(decision_dates))
    sqlite_cache_dir = root / DEFAULT_SQLITE_CACHE_DIR
    sqlite_path = sqlite_cache_dir / "market.sqlite"
    calendar_days = read_market_calendar(sqlite_path, min(decision_dates), end)
    bars = read_daily_bars(sqlite_path, start, end)
    warnings: list[str] = []
    token = env.get("JQUANTS_API_KEY")
    if (calendar_days is None or bars is None) and token and allow_provider:
        # cache_dir / sqlite_cache_dir は固定の相対 path (env override 廃止)。
        # 詳細は screening/config.py の同名コメント参照。`root` 配下に解決する
        # ことで、test 等で workspace を切り替えるユースケースにも対応する。
        cache_dir = root / DEFAULT_CACHE_DIR
        provider = JQuantsMarketProvider(
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
            warnings.append(f"failed to load J-Quants market data: {exc}")
    elif bars is None:
        if allow_provider and not token:
            warnings.append("JQUANTS_API_KEY is unset and SQLite has no bars")
        else:
            warnings.append(
                f"SQLite has no J-Quants bars for {start.isoformat()}..{end.isoformat()}"
            )
    calendar = _business_calendar(calendar_days)
    if not bars:
        warnings.append("no J-Quants bars were loaded; holding prices stay unfilled")
    elif require_calendar and not calendar:
        warnings.append("J-Quants bars loaded but no business calendar was loaded")
    return calendar, tuple(bars or ()), tuple(warnings)


def _business_calendar(
    calendar_days: list[JQuantsMarketCalendarDay] | None,
) -> tuple[date, ...]:
    if calendar_days is None:
        return ()
    return tuple(day.day for day in calendar_days if day.is_business_day)


def _discover_decision_dates(root: Path) -> tuple[date, ...]:
    dates: list[date] = []
    for path in sorted((root / "records/03-thesis").rglob("*.md")):
        try:
            dates.append(date.fromisoformat(path.name[:10]))
        except ValueError:
            continue
    return tuple(dates)


if __name__ == "__main__":
    raise SystemExit(main())
