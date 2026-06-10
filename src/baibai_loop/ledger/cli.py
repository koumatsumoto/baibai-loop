from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import yaml

from baibai_loop._env import load_project_env
from baibai_loop.screening.config import DEFAULT_CACHE_DIR, DEFAULT_SQLITE_CACHE_DIR
from baibai_loop.screening.providers.jquants import (
    JQuantsDailyBar,
    JQuantsMarketCalendarDay,
    JQuantsProvider,
    JQuantsProviderError,
)
from baibai_loop.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules
from baibai_loop.screening.sqlite_reader import read_daily_bars, read_market_calendar

from .benchmark import NIKKEI225_ETF_PROXY, PortfolioBenchmark, compute_forward_performance
from .lane_cohorts import (
    DEFAULT_COHORT_HORIZON_WEEKS,
    lane_cohorts_to_payload,
    render_lane_cohort_summary,
    run_lane_cohorts,
)
from .market_data import PriceObservation, load_fallback_price_observations
from .retro import build_monthly_retro, write_monthly_retro
from .review_gates import ReviewGate, due_review_gates, weekday_calendar
from .screening_replay import discover_week_specs, replay_to_payload, run_replay
from .selection_ablation import (
    ablation_to_payload,
    render_ablation_summary,
    run_selection_ablation,
)
from .sync import sync_ledger
from .trades import load_open_trades


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
    gates_parser = subparsers.add_parser(
        "review-gates",
        help="list open-position forward review gates (+15bd/+30bd) that are due",
    )
    gates_parser.add_argument("--root", type=Path, default=Path.cwd())
    gates_parser.add_argument("--asof", help="evaluation date (YYYY-MM-DD); defaults to today")
    benchmark_parser = subparsers.add_parser(
        "benchmark",
        help="compute open-position forward return versus the Nikkei 225 ETF proxy",
    )
    benchmark_parser.add_argument("--root", type=Path, default=Path.cwd())
    benchmark_parser.add_argument("--asof", help="evaluation date (YYYY-MM-DD); defaults to today")
    benchmark_parser.add_argument(
        "--proxy",
        default=NIKKEI225_ETF_PROXY,
        help=f"benchmark ETF proxy ticker (default: {NIKKEI225_ETF_PROXY})",
    )
    replay_parser = subparsers.add_parser(
        "screening-replay",
        help="replay selection profiles over weekly candidates and score forward return",
    )
    replay_parser.add_argument("--root", type=Path, default=Path.cwd())
    replay_parser.add_argument(
        "--candidates-root",
        type=Path,
        required=True,
        help="root holding weekly candidate YAML in <YYYY>/<MM>/<YYYY-MM-DD>.yaml layout",
    )
    replay_parser.add_argument(
        "--profiles",
        default="strict,balanced,loose",
        help="comma-separated selection profiles (default: strict,balanced,loose)",
    )
    replay_parser.add_argument("--top", type=int, default=10, help="candidates per profile")
    replay_parser.add_argument(
        "--holdout-weeks", type=int, default=2, help="trailing weeks to flag as hold-out"
    )
    replay_parser.add_argument(
        "--out", type=Path, help="write replay payload YAML to this path instead of stdout"
    )
    replay_parser.add_argument(
        "--rules-path", type=Path, default=DEFAULT_RULES_PATH, help="screening rules path"
    )
    replay_parser.add_argument(
        "--regime-lens",
        choices=("on", "off"),
        default="on",
        help="apply the market regime lens per replay week (default: on)",
    )
    cohort_parser = subparsers.add_parser(
        "lane-cohorts",
        help="aggregate forward returns per evidence lane over all weekly candidates",
    )
    cohort_parser.add_argument("--root", type=Path, default=Path.cwd())
    cohort_parser.add_argument(
        "--candidates-root",
        type=Path,
        required=True,
        help="root holding weekly candidate YAML in <YYYY>/<MM>/<YYYY-MM-DD>.yaml layout",
    )
    cohort_parser.add_argument(
        "--horizons",
        default=",".join(str(weeks) for weeks in DEFAULT_COHORT_HORIZON_WEEKS),
        help="comma-separated forward horizons in calendar weeks (default: 1,4)",
    )
    cohort_parser.add_argument(
        "--out", type=Path, help="write cohort payload YAML to this path instead of stdout"
    )
    ablation_parser = subparsers.add_parser(
        "selection-ablation",
        help="replay selection variants that disable one feature each and score forward return",
    )
    ablation_parser.add_argument("--root", type=Path, default=Path.cwd())
    ablation_parser.add_argument(
        "--candidates-root",
        type=Path,
        required=True,
        help="root holding weekly candidate YAML in <YYYY>/<MM>/<YYYY-MM-DD>.yaml layout",
    )
    ablation_parser.add_argument(
        "--top", type=int, default=5, help="recommended queue size per variant (default 5)"
    )
    ablation_parser.add_argument(
        "--profile", help="selection profile (default: rules.selection.default_profile)"
    )
    ablation_parser.add_argument(
        "--horizons",
        default="1,4",
        help="comma-separated forward horizons in calendar weeks (default: 1,4)",
    )
    ablation_parser.add_argument(
        "--rules-path", type=Path, default=DEFAULT_RULES_PATH, help="screening rules path"
    )
    ablation_parser.add_argument(
        "--out", type=Path, help="write ablation payload YAML to this path instead of stdout"
    )
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
    if args.command == "review-gates":
        return _run_review_gates(args.root, _resolve_asof(args.asof))
    if args.command == "benchmark":
        return _run_benchmark(args.root, _resolve_asof(args.asof), args.proxy, os.environ)
    if args.command == "screening-replay":
        return _run_screening_replay(args)
    if args.command == "lane-cohorts":
        return _run_lane_cohorts(args)
    if args.command == "selection-ablation":
        return _run_selection_ablation(args)
    raise AssertionError(f"unreachable command: {args.command!r}")


def _run_selection_ablation(args: argparse.Namespace) -> int:
    horizons = _parse_horizons(args.horizons)
    if horizons is None:
        return 1
    weeks = discover_week_specs(args.candidates_root)
    if not weeks:
        print(f"no weekly candidate files under {args.candidates_root}", file=sys.stderr)
        return 1
    sqlite_path = args.root / DEFAULT_SQLITE_CACHE_DIR / "market.sqlite"
    result = run_selection_ablation(
        weeks,
        rules=load_screening_rules(args.rules_path),
        sqlite_path=sqlite_path,
        candidates_root=args.candidates_root,
        ledger_root=args.root / "records",
        profile=args.profile,
        top=args.top,
        horizon_weeks=horizons,
    )
    if args.out is not None:
        text = yaml.safe_dump(ablation_to_payload(result), allow_unicode=True, sort_keys=False)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    print(render_ablation_summary(result))
    return 0


def _parse_horizons(raw: str) -> tuple[int, ...] | None:
    try:
        horizons = tuple(int(token.strip()) for token in str(raw).split(",") if token.strip())
    except ValueError:
        print("--horizons must be comma-separated integers", file=sys.stderr)
        return None
    if not horizons or any(weeks < 1 for weeks in horizons):
        print("--horizons must include at least one positive week count", file=sys.stderr)
        return None
    return horizons


def _run_lane_cohorts(args: argparse.Namespace) -> int:
    horizons = _parse_horizons(args.horizons)
    if horizons is None:
        return 1
    weeks = discover_week_specs(args.candidates_root)
    if not weeks:
        print(f"no weekly candidate files under {args.candidates_root}", file=sys.stderr)
        return 1
    sqlite_path = args.root / DEFAULT_SQLITE_CACHE_DIR / "market.sqlite"
    result = run_lane_cohorts(
        weeks,
        sqlite_path=sqlite_path,
        horizon_weeks=horizons,
    )
    if args.out is not None:
        payload = lane_cohorts_to_payload(result)
        text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    print(render_lane_cohort_summary(result))
    return 0


def _run_screening_replay(args: argparse.Namespace) -> int:
    profiles = [profile.strip() for profile in args.profiles.split(",") if profile.strip()]
    if not profiles:
        print("--profiles must include at least one profile", file=sys.stderr)
        return 1
    weeks = discover_week_specs(args.candidates_root, holdout_weeks=args.holdout_weeks)
    if not weeks:
        print(f"no weekly candidate files under {args.candidates_root}", file=sys.stderr)
        return 1
    sqlite_path = args.root / DEFAULT_SQLITE_CACHE_DIR / "market.sqlite"
    result = run_replay(
        weeks,
        profiles=profiles,
        rules=load_screening_rules(args.rules_path),
        sqlite_path=sqlite_path,
        candidates_root=args.candidates_root,
        ledger_root=args.root / "records",
        top=args.top,
        regime_lens=args.regime_lens == "on",
    )
    payload = replay_to_payload(result)
    payload["weeks_meta"] = [
        {"week": spec.asof.isoformat(), "is_holdout": spec.is_holdout} for spec in weeks
    ]
    text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text, end="")
    return 0


def _resolve_asof(value: str | None) -> date:
    if value is None:
        return datetime.now(UTC).date()
    return date.fromisoformat(value)


def _run_review_gates(root: Path, asof: date) -> int:
    trades = load_open_trades(root)
    if not trades:
        print("no open positions")
        return 0
    sqlite_path = root / DEFAULT_SQLITE_CACHE_DIR / "market.sqlite"
    earliest_entry = min(trade.entry_date for trade in trades)
    calendar_days = read_market_calendar(sqlite_path, earliest_entry, asof)
    calendar: Sequence[date]
    if calendar_days is None:
        # +30bd needs roughly six weeks of forward calendar beyond entry; extend
        # the weekday fallback so distant targets still resolve to a date.
        calendar = weekday_calendar(earliest_entry, max(asof, earliest_entry + timedelta(days=70)))
    else:
        calendar = tuple(day.day for day in calendar_days if day.is_business_day)
    gates = due_review_gates(trades, asof, calendar)
    print(f"asof={asof.isoformat()} open_positions={len(trades)} due_gates={len(gates)}")
    for gate in gates:
        print(_format_gate(gate))
    return 0


def _format_gate(gate: ReviewGate) -> str:
    return (
        f"due {gate.horizon} target={gate.target_date.isoformat()} "
        f"{gate.ticker} {gate.name} entry={gate.entry_date.isoformat()} "
        f"review_state={gate.review_state}"
    )


def _run_benchmark(root: Path, asof: date, proxy: str, env: Mapping[str, str]) -> int:
    trades = load_open_trades(root)
    if not trades:
        print("no open positions")
        return 0
    _calendar, bars, _fallback, market_warnings = _load_market_data(root, env)
    for warning in market_warnings:
        print(f"warning: {warning}", file=sys.stderr)
    result = compute_forward_performance(trades, asof, bars, proxy)
    for line in _format_benchmark(result):
        print(line)
    for warning in result.warnings:
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
