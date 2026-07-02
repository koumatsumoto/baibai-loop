"""Argument parser and dispatch for the screening CLI."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from baibai_loop.foundation.env import load_project_env
from baibai_loop.screening.config import (
    DEFAULT_SQLITE_CACHE_DIR,
    ConfigError,
    ScreeningConfig,
)
from baibai_loop.screening.providers import EDINETProvider, JPXProvider, JQuantsProvider
from baibai_loop.screening.rule_config import (
    DEFAULT_RULES_PATH,
    load_screening_rules,
)
from baibai_loop.screening.sqlite_coverage import (
    verify_screening_sqlite_coverage,
)

from .cache import (
    _print_cache_coverage_issues,
    bootstrap_cache_command,
    extract_edinet_metrics_command,
    verify_cache_coverage_command,
)
from .common import _parse_iso_date
from .providers import ProviderBundle
from .query import (
    market_snapshot_command,
    select_command,
    ticker_profile_command,
)
from .run import run_command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m baibai_loop.screening.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run weekly screening")
    run_parser.add_argument("--asof", required=True, help="screening target date (YYYY-MM-DD)")
    run_parser.add_argument(
        "--allow-stale-jpx",
        action="store_true",
        help="allow an already-cached stale JPX snapshot for a historical backfill asof",
    )
    run_parser.add_argument(
        "--output-path",
        help=(
            "write candidates YAML to this path instead of the canonical "
            "records/02-candidates/YYYY/MM/YYYY-MM-DD.yaml path"
        ),
    )
    run_parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing candidates YAML output path",
    )

    bootstrap_parser = subparsers.add_parser(
        "bootstrap-cache",
        help="bootstrap canonical SQLite inputs for a screening as-of date",
    )
    bootstrap_parser.add_argument(
        "--asof",
        required=True,
        help="screening target date (YYYY-MM-DD); computes each source window automatically",
    )

    extract_parser = subparsers.add_parser(
        "extract-edinet-metrics",
        help="extract EDINET type=5 CSV metrics into canonical SQLite",
    )
    extract_parser.add_argument("--asof", required=True, help="metrics as-of date (YYYY-MM-DD)")
    extract_parser.add_argument(
        "--lookback-days",
        type=int,
        default=540,
        help="EDINET document-list lookback window in calendar days (default 540)",
    )

    coverage_parser = subparsers.add_parser(
        "verify-cache-coverage",
        help="check that SQLite can serve all local inputs required by screening run",
    )
    coverage_parser.add_argument("--asof", required=True, help="screening target date (YYYY-MM-DD)")
    coverage_parser.add_argument(
        "--sqlite-path",
        default=str(DEFAULT_SQLITE_CACHE_DIR / "market.sqlite"),
        help=f"SQLite cache path (default: {DEFAULT_SQLITE_CACHE_DIR}/market.sqlite)",
    )
    coverage_parser.add_argument(
        "--rules-path",
        default=str(DEFAULT_RULES_PATH),
        help=f"screening rules path for required JPX sources (default: {DEFAULT_RULES_PATH})",
    )
    coverage_parser.add_argument(
        "--allow-stale-jpx",
        action="store_true",
        help=(
            "degraded verification only; allow old JPX fetched_at_utc snapshots "
            "(use together with run --allow-stale-jpx)"
        ),
    )

    select_parser = subparsers.add_parser(
        "select",
        help="rank research candidates by combining candidates with macro context",
    )
    select_parser.add_argument("--asof", required=True, help="screening target date (YYYY-MM-DD)")
    select_parser.add_argument(
        "--macro-context",
        help=(
            "macro context path to apply (default: latest "
            "records/01-macro-context/<YYYY>/<MM>/macro-context-*.yaml on or before asof)"
        ),
    )
    select_parser.add_argument(
        "--candidates",
        help=(
            "candidates YAML path to rank (default: "
            "records/02-candidates/<YYYY>/<MM>/<YYYY-MM-DD>.yaml)"
        ),
    )
    select_parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="maximum number of candidates to emit (default 10)",
    )
    select_parser.add_argument(
        "--rules-path",
        default=str(DEFAULT_RULES_PATH),
        help=f"screening rules path (default: {DEFAULT_RULES_PATH})",
    )
    select_parser.add_argument(
        "--profile",
        help="selection profile to apply (default: rules.selection.default_profile)",
    )
    select_parser.add_argument(
        "--detail",
        choices=("summary", "full"),
        default="summary",
        help="selection output detail (default: summary)",
    )
    _add_market_state_arguments(select_parser)

    profile_parser = subparsers.add_parser(
        "ticker-profile",
        help="emit the single-ticker fact packet (price, relative, regime, events, screening)",
    )
    profile_parser.add_argument("--ticker", required=True, help="4-character ticker code")
    profile_parser.add_argument(
        "--asof",
        help="evaluation date (YYYY-MM-DD; default: latest cached trading day)",
    )
    profile_parser.add_argument(
        "--sqlite-path",
        default=str(DEFAULT_SQLITE_CACHE_DIR / "market.sqlite"),
        help=f"SQLite cache path (default: {DEFAULT_SQLITE_CACHE_DIR}/market.sqlite)",
    )
    profile_parser.add_argument(
        "--candidates-root",
        default="records/02-candidates",
        help="root of recorded candidates YAML (default: records/02-candidates)",
    )
    profile_parser.add_argument(
        "--root",
        default=".",
        help="repository root for records lookups (default: current directory)",
    )

    snapshot_parser = subparsers.add_parser(
        "market-snapshot",
        help="emit the market state packet (weekly regime history and sector aggregates)",
    )
    snapshot_parser.add_argument(
        "--asof",
        help="evaluation date (YYYY-MM-DD; default: latest cached trading day)",
    )
    snapshot_parser.add_argument(
        "--weeks",
        type=int,
        default=12,
        help="number of weekly history points (default: 12)",
    )
    snapshot_parser.add_argument(
        "--sqlite-path",
        default=str(DEFAULT_SQLITE_CACHE_DIR / "market.sqlite"),
        help=f"SQLite cache path (default: {DEFAULT_SQLITE_CACHE_DIR}/market.sqlite)",
    )

    return parser


def _add_market_state_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--sqlite-path",
        default=str(DEFAULT_SQLITE_CACHE_DIR / "market.sqlite"),
        help=(
            "SQLite cache used to compute the market state fact "
            "(benchmark return / regime label; absence degrades to null) "
            f"(default: {DEFAULT_SQLITE_CACHE_DIR}/market.sqlite)"
        ),
    )


def main(argv: list[str] | None = None) -> int:
    load_project_env()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "select":
        # select reads existing candidates YAML, macro context YAML, and the
        # local rule config for output parameters; no provider credentials are
        # needed.
        return select_command(
            asof_date=_parse_iso_date(args.asof),
            candidates_path=Path(args.candidates) if args.candidates else None,
            macro_context_path=Path(args.macro_context) if args.macro_context else None,
            top=args.top,
            rules=load_screening_rules(Path(args.rules_path)),
            profile=args.profile,
            detail=args.detail,
            regime_sqlite_path=Path(args.sqlite_path),
        )

    if args.command == "ticker-profile":
        # ticker-profile reads the SQLite store and recorded output only; no
        # provider credentials are needed.
        return ticker_profile_command(
            ticker=args.ticker,
            asof=args.asof,
            sqlite_path=Path(args.sqlite_path),
            candidates_root=Path(args.candidates_root),
            records_root=Path(args.root) / "records",
        )

    if args.command == "market-snapshot":
        # market-snapshot reads the SQLite store only; no provider credentials needed.
        return market_snapshot_command(
            asof=args.asof,
            weeks=args.weeks,
            sqlite_path=Path(args.sqlite_path),
        )

    if args.command == "verify-cache-coverage":
        # verify-cache-coverage is local-only and never reads raw JSON or calls providers.
        rules = load_screening_rules(Path(args.rules_path))
        return verify_cache_coverage_command(
            sqlite_path=Path(args.sqlite_path),
            asof_date=_parse_iso_date(args.asof),
            required_jpx_sources=rules.universe.required_jpx_flags,
            allow_stale_jpx=args.allow_stale_jpx,
        )

    try:
        config = ScreeningConfig.from_env()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    sqlite_path = config.sqlite_cache_dir / "market.sqlite"
    run_asof_date = _parse_iso_date(args.asof) if args.command == "run" else None
    run_rules = load_screening_rules(config.rules_path) if args.command == "run" else None
    if args.command == "run":
        if run_asof_date is None:
            print("--asof is required for run", file=sys.stderr)
            return 1
        coverage_issues = verify_screening_sqlite_coverage(
            sqlite_path,
            run_asof_date,
            required_jpx_sources=run_rules.universe.required_jpx_flags if run_rules else (),
            allow_stale_jpx=args.allow_stale_jpx,
        )
        if coverage_issues:
            _print_cache_coverage_issues(
                coverage_issues, asof_date=run_asof_date, stream=sys.stderr
            )
            return 1
    providers = ProviderBundle(
        jquants=JQuantsProvider(
            config.jquants_api_key,
            config.cache_dir,
            sqlite_path=sqlite_path,
            cache_only=args.command == "run",
        ),
        edinet=(
            EDINETProvider(
                config.edinet_api_key,
                config.cache_dir,
                sqlite_path=sqlite_path,
                cache_only=args.command == "run",
            )
            if config.edinet_api_key or args.command == "run"
            else None
        ),
        jpx=JPXProvider(
            config.cache_dir,
            regulation_urls=config.jpx_regulation_urls,
            special_caution_index_url=config.jpx_special_caution_index_url,
            sqlite_path=sqlite_path,
            cache_only=args.command == "run",
        ),
    )

    if args.command == "run":
        if run_asof_date is None:
            print("--asof is required for run", file=sys.stderr)
            return 1
        return run_command(
            run_asof_date,
            config,
            providers,
            rules=run_rules,
            allow_stale_jpx=args.allow_stale_jpx,
            output_path=Path(args.output_path) if args.output_path else None,
            force=args.force,
        )

    if args.command == "bootstrap-cache":
        return bootstrap_cache_command(
            asof_date=_parse_iso_date(args.asof),
            providers=providers,
        )

    if args.command == "extract-edinet-metrics":
        if args.lookback_days < 0:
            print("--lookback-days must be zero or greater", file=sys.stderr)
            return 1
        if providers.edinet is None:
            print("EDINET_API_KEY is required for extract-edinet-metrics", file=sys.stderr)
            return 1
        return extract_edinet_metrics_command(
            asof_date=_parse_iso_date(args.asof),
            lookback_days=args.lookback_days,
            provider=providers.edinet,
            sqlite_path=sqlite_path,
        )

    raise AssertionError(f"unreachable command: {args.command!r}")
