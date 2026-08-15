"""Argument parser and dispatch for the screening CLI."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import cast

from baibai_engine.foundation.env import load_project_env
from baibai_engine.foundation.repository_layout import APPLICATION_DB_PATH, RUNS_DB_PATH
from baibai_engine.foundation.time import JST
from baibai_engine.screening.calibration.cli import (
    calibration_build_command,
    calibration_evaluate_command,
)
from baibai_engine.screening.calibration.grid import days_with_bars, month_end_asof_grid
from baibai_engine.screening.calibration.legacy_csv import migrate_legacy_calibration
from baibai_engine.screening.calibration.panel import PANEL_BUILD_POLICIES, PanelVariant
from baibai_engine.screening.calibration.store import (
    DEFAULT_CALIBRATION_DIR,
    CalibrationCacheError,
)
from baibai_engine.screening.config import (
    DEFAULT_SQLITE_CACHE_DIR,
    ConfigError,
    ScreeningConfig,
)
from baibai_engine.screening.providers import EDINETProvider, JPXProvider, JQuantsProvider
from baibai_engine.screening.rule_config import (
    DEFAULT_RULES_PATH,
    load_screening_rules,
)
from baibai_engine.screening.sqlite_coverage import (
    verify_screening_sqlite_coverage,
)

from .cache import (
    _print_cache_coverage_issues,
    backfill_history_command,
    backfill_master_command,
    bootstrap_cache_command,
    invalidate_coverage_command,
    refresh_buyback_reports_command,
    refresh_edinet_documents_command,
    verify_cache_coverage_command,
)
from .capital_control_cli import (
    backfill_edinet_identity_command,
    build_control_event_exits_command,
    refresh_capital_control_command,
)
from .common import _parse_iso_date
from .edinet_extract import extract_edinet_metrics_command
from .providers import ProviderBundle
from .prune import prune_command
from .query import (
    market_snapshot_command,
    select_command,
    selection_show_command,
    ticker_profile_command,
)
from .run import run_command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine screening")
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
        help=("also write the DB publication view as local YAML to this path"),
    )
    run_parser.add_argument("--runs-db", help="screening run store path")
    run_parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing candidates YAML output path",
    )

    prune_parser = subparsers.add_parser(
        "prune",
        help="delete old screening run cache generations and vacuum the store",
    )
    prune_parser.add_argument(
        "--keep",
        type=int,
        default=3,
        help="number of newest run generations to keep (default: 3)",
    )
    prune_parser.add_argument("--runs-db", help="screening run store path")

    bootstrap_parser = subparsers.add_parser(
        "bootstrap-cache",
        help="bootstrap canonical SQLite inputs for a screening as-of date",
    )
    bootstrap_parser.add_argument(
        "--asof",
        required=True,
        help="screening target date (YYYY-MM-DD); computes each source window automatically",
    )

    refresh_edinet_parser = subparsers.add_parser(
        "refresh-edinet-documents",
        help="refresh mutable and unresolved EDINET document-list state",
    )
    refresh_edinet_parser.add_argument(
        "--asof",
        required=True,
        help="target date (YYYY-MM-DD); refreshes current source state, not a point-in-time view",
    )

    backfill_history_parser = subparsers.add_parser(
        "backfill-history",
        help=("fetch range sources and margin balances over an explicit window"),
    )
    backfill_history_parser.add_argument(
        "--start", required=True, help="first date the window covers (YYYY-MM-DD)"
    )
    backfill_history_parser.add_argument(
        "--end", required=True, help="last date the window covers (YYYY-MM-DD)"
    )
    backfill_history_parser.add_argument(
        "--probe-margin-publication-transition",
        action="store_true",
        help=(
            "one-shot U4 probe: fetch only the 2026-09-25 all-issues daily margin "
            "before runtime activation, after official go-live"
        ),
    )

    backfill_master_parser = subparsers.add_parser(
        "backfill-master",
        help="store the point-in-time security master for one or more as-of dates",
    )
    backfill_master_parser.add_argument(
        "--asof",
        action="append",
        dest="asofs",
        help="master snapshot date (YYYY-MM-DD; repeatable)",
    )
    backfill_master_parser.add_argument(
        "--month-end-from",
        help="add every calendar month end from this date through --month-end-to (YYYY-MM-DD)",
    )
    backfill_master_parser.add_argument(
        "--month-end-to",
        help="last date considered for --month-end-from (YYYY-MM-DD)",
    )

    extract_parser = subparsers.add_parser(
        "extract-edinet-metrics",
        help=(
            "extract EDINET type=5 CSV metrics; historical cache misses use current source state"
        ),
    )
    extract_parser.add_argument(
        "--asof",
        required=True,
        help="metrics as-of date (YYYY-MM-DD), not a point-in-time EDINET reconstruction",
    )
    extract_parser.add_argument(
        "--lookback-days",
        type=int,
        default=540,
        help="EDINET document-list lookback window in calendar days (default 540)",
    )

    buyback_parser = subparsers.add_parser(
        "refresh-buyback-reports",
        help="read the buyback authorization state out of listed form-220 filings",
    )
    buyback_parser.add_argument(
        "--asof",
        required=True,
        help="latest reporting date to consider (YYYY-MM-DD)",
    )
    buyback_parser.add_argument(
        "--lookback-days",
        type=int,
        default=540,
        help="how far back to read filings from --asof, in calendar days (default 540)",
    )
    buyback_parser.add_argument(
        "--sqlite-path",
        default=str(DEFAULT_SQLITE_CACHE_DIR / "market.sqlite"),
        help=f"SQLite cache path (default: {DEFAULT_SQLITE_CACHE_DIR}/market.sqlite)",
    )

    capital_control_parser = subparsers.add_parser(
        "refresh-capital-control",
        help="re-read the TSE capital-policy disclosure list and the JPX delisting record",
    )
    capital_control_parser.add_argument(
        "--asof",
        required=True,
        help="date the EDINET event histogram is reported through (YYYY-MM-DD)",
    )
    capital_control_parser.add_argument(
        "--sqlite-path",
        default=str(DEFAULT_SQLITE_CACHE_DIR / "market.sqlite"),
        help=f"SQLite cache path (default: {DEFAULT_SQLITE_CACHE_DIR}/market.sqlite)",
    )

    identity_parser = subparsers.add_parser(
        "backfill-edinet-identity",
        help="re-list EDINET days so stored rows carry submitter and target company codes",
    )
    identity_parser.add_argument("--start", required=True, help="first day to re-list (YYYY-MM-DD)")
    identity_parser.add_argument("--end", required=True, help="last day to re-list (YYYY-MM-DD)")

    exits_parser = subparsers.add_parser(
        "build-control-event-exits",
        help="derive realized tender-offer exit prices for delisted names",
    )
    exits_parser.add_argument(
        "--asof",
        required=True,
        help="latest delisting date to resolve (YYYY-MM-DD)",
    )
    exits_parser.add_argument(
        "--sqlite-path",
        default=str(DEFAULT_SQLITE_CACHE_DIR / "market.sqlite"),
        help=f"SQLite cache path (default: {DEFAULT_SQLITE_CACHE_DIR}/market.sqlite)",
    )

    invalidate_parser = subparsers.add_parser(
        "invalidate-coverage",
        help="delete source_coverage rows so the next bootstrap-cache refetches a source",
    )
    invalidate_parser.add_argument(
        "--source",
        required=True,
        help="coverage source name to invalidate (rejected with the known list if unknown)",
    )
    invalidate_parser.add_argument(
        "--start",
        help="restrict to coverage windows overlapping from this date (YYYY-MM-DD)",
    )
    invalidate_parser.add_argument(
        "--end",
        help="restrict to coverage windows overlapping to this date (YYYY-MM-DD)",
    )
    invalidate_parser.add_argument(
        "--sqlite-path",
        default=str(DEFAULT_SQLITE_CACHE_DIR / "market.sqlite"),
        help=f"SQLite cache path (default: {DEFAULT_SQLITE_CACHE_DIR}/market.sqlite)",
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
        default=os.environ.get("SCREENING_RULES_PATH") or str(DEFAULT_RULES_PATH),
        help=f"screening rules path for required JPX sources "
        f"(default: SCREENING_RULES_PATH or {DEFAULT_RULES_PATH})",
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
        help="rank research candidates and optionally summarize macro material deltas",
    )
    select_parser.add_argument("--asof", required=True, help="screening target date (YYYY-MM-DD)")
    select_parser.add_argument(
        "--run-revision-id",
        required=True,
        help="immutable screening run revision to select from",
    )
    select_parser.add_argument("--runs-db", help="screening run store path")
    select_parser.add_argument(
        "--previous-run-revision-id",
        help="explicit previous revision when the greatest prior as-of is ambiguous",
    )
    select_parser.add_argument(
        "--previous-shortlist-id",
        help="canonical shortlist whose retained entries replace a pruned previous run",
    )
    select_parser.add_argument(
        "--longlist-history-dir",
        help=(
            "persisted daily longlist records, used as the previous candidate set "
            "when the prior as-of has been pruned out of the run store"
        ),
    )
    select_parser.add_argument("--app-db", help="application DB path")
    select_parser.add_argument(
        "--macro-context-id",
        help="published macro context ID (default: latest eligible context)",
    )
    select_parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="maximum number of candidates to emit (default 10)",
    )
    select_parser.add_argument(
        "--rules-path",
        default=os.environ.get("SCREENING_RULES_PATH") or str(DEFAULT_RULES_PATH),
        help=f"screening rules path (default: SCREENING_RULES_PATH or {DEFAULT_RULES_PATH})",
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
    select_parser.add_argument(
        "--longlist-top",
        type=int,
        default=0,
        help=(
            "emit an longlist of the top N ranked candidates before diversity/cap "
            "truncation (0-100; default 0 omits longlist for output compatibility)"
        ),
    )
    select_parser.add_argument(
        "--output-path",
        help="also write the selection YAML to this path (stdout is unchanged)",
    )
    select_parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing --output-path file",
    )
    _add_market_state_arguments(select_parser)

    selection_parser = subparsers.add_parser(
        "selection",
        help="read published selections",
    )
    selection_commands = selection_parser.add_subparsers(dest="selection_command", required=True)
    selection_show = selection_commands.add_parser(
        "show",
        help="re-emit a published selection output without publishing a new one",
    )
    selection_show.add_argument("--selection-id", required=True)
    selection_show.add_argument("--runs-db", help="screening run store path")
    selection_show.add_argument(
        "--output-path",
        help="also write the selection YAML to this path (stdout is unchanged)",
    )
    selection_show.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing --output-path file",
    )

    shortlist_parser = subparsers.add_parser(
        "shortlist",
        help="publish a shortlist judgment",
    )
    shortlist_commands = shortlist_parser.add_subparsers(dest="shortlist_command", required=True)
    shortlist_preflight = shortlist_commands.add_parser(
        "preflight",
        help="choose cloud-result reuse or one current-code rerun before consuming retention",
    )
    shortlist_preflight.add_argument("--asof", required=True, help="target date (YYYY-MM-DD)")
    shortlist_preflight.add_argument(
        "--cloud-summary",
        required=True,
        help="latest workflow run summary downloaded with r2_transfer.sh pull-run-summary",
    )
    shortlist_preflight.add_argument("--db", help="application DB path")
    shortlist_preflight.add_argument("--runs-db", help="screening run store path")
    shortlist_preflight.add_argument(
        "--previous-run-revision-id",
        help="explicit greatest-prior run when preflight reports ambiguous previous publications",
    )
    shortlist_preflight.add_argument(
        "--repo-root", default=".", help="repository root whose checked-out commit is evaluated"
    )
    shortlist_publish = shortlist_commands.add_parser(
        "publish",
        help="publish a shortlist draft as an immutable judgment bound to a screening run",
    )
    shortlist_publish.add_argument("draft")
    shortlist_publish.add_argument("--db", help="application DB path")
    shortlist_publish.add_argument("--runs-db", help="screening run store path")
    shortlist_outcome = shortlist_commands.add_parser(
        "outcome",
        help="compare each published shortlist's selected / rejected / machine cohorts",
    )
    shortlist_outcome.add_argument("--db", help="application DB path")
    shortlist_outcome.add_argument("--runs-db", help="screening run store path")
    shortlist_outcome.add_argument(
        "--sqlite-path",
        default=str(DEFAULT_SQLITE_CACHE_DIR / "market.sqlite"),
        help=f"SQLite cache path (default: {DEFAULT_SQLITE_CACHE_DIR}/market.sqlite)",
    )
    shortlist_outcome.add_argument(
        "--horizon",
        action="append",
        dest="horizons",
        help="horizon to evaluate (3m/6m/1y/3y/5y; repeatable)",
    )
    shortlist_outcome.add_argument("--out", help="write the YAML payload to this path")

    profile_parser = subparsers.add_parser(
        "ticker-profile",
        help="emit the single-ticker fact profile (price, relative, regime, events, screening)",
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
        "--runs-db",
        help="screening run store path",
    )
    profile_parser.add_argument(
        "--app-db",
        help="application DB path",
    )
    profile_parser.add_argument(
        "--run-revision-id",
        help="explicit screening run revision (default: latest stored run)",
    )

    calibration_build_parser = subparsers.add_parser(
        "calibration-build",
        help=(
            "build point-in-time monthly panels and forward returns for "
            "estimate calibration (local cache only)"
        ),
    )
    calibration_build_parser.add_argument(
        "--start", required=True, help="grid start date (YYYY-MM-DD)"
    )

    calibration_migrate_parser = subparsers.add_parser(
        "calibration-migrate-legacy",
        help="archive legacy calibration CSV bytes and migrate compatible cohorts to L2",
    )
    calibration_migrate_parser.add_argument("--legacy-dir", type=Path, required=True)
    calibration_migrate_parser.add_argument(
        "--calibration-dir", type=Path, default=DEFAULT_CALIBRATION_DIR
    )
    calibration_migrate_parser.add_argument("--report", type=Path, required=True)
    calibration_build_parser.add_argument("--end", required=True, help="grid end date (YYYY-MM-DD)")
    calibration_build_parser.add_argument(
        "--sqlite-path",
        default=str(DEFAULT_SQLITE_CACHE_DIR / "market.sqlite"),
        help=f"SQLite cache path (default: {DEFAULT_SQLITE_CACHE_DIR}/market.sqlite)",
    )
    calibration_build_parser.add_argument(
        "--rules-path",
        default=os.environ.get("SCREENING_RULES_PATH") or str(DEFAULT_RULES_PATH),
        help=f"screening rules path (default: SCREENING_RULES_PATH or {DEFAULT_RULES_PATH})",
    )
    calibration_build_parser.add_argument(
        "--calibration-dir",
        default=str(DEFAULT_CALIBRATION_DIR),
        help=f"panel / forward store directory (default: {DEFAULT_CALIBRATION_DIR})",
    )
    calibration_build_parser.add_argument(
        "--force",
        action="store_true",
        help="rebuild panels that already exist in the calibration store",
    )
    calibration_build_parser.add_argument(
        "--panel-variant",
        choices=tuple(PANEL_BUILD_POLICIES),
        default="production",
        help="panel input contract (non-production variants are diagnostic-only)",
    )
    calibration_build_parser.add_argument(
        "--replace-broken-current",
        action="store_true",
        help=(
            "quarantine an unreadable calibration root and rebuild from empty; without "
            "this, an unreadable root stops the build rather than being overwritten"
        ),
    )
    calibration_build_parser.add_argument(
        "--without-control-event-exits",
        action="store_true",
        help=(
            "resolve forward windows from market closes alone, leaving delisted names to "
            "the imputation bracket; regenerates the comparison baseline"
        ),
    )

    calibration_evaluate_parser = subparsers.add_parser(
        "calibration-evaluate",
        help="evaluate stored calibration cohorts (rank IC / decile / selection replay)",
    )
    calibration_evaluate_parser.add_argument(
        "--calibration-dir",
        default=str(DEFAULT_CALIBRATION_DIR),
        help=f"panel / forward store directory (default: {DEFAULT_CALIBRATION_DIR})",
    )
    calibration_evaluate_parser.add_argument(
        "--horizon",
        action="append",
        dest="horizons",
        help="horizon to evaluate (3m/6m/1y/3y/5y; repeatable; default: all)",
    )
    calibration_evaluate_parser.add_argument(
        "--run-purpose", choices=("diagnostic", "production_decision"), default="diagnostic"
    )
    calibration_evaluate_parser.add_argument(
        "--required-asof", action="append", dest="required_asofs"
    )
    calibration_evaluate_parser.add_argument(
        "--required-metric", action="append", dest="required_metrics"
    )
    calibration_evaluate_parser.add_argument(
        "--out",
        help="write the evaluation YAML to this path instead of stdout",
    )
    calibration_evaluate_parser.add_argument(
        "--context-out",
        help="write the expiring E[r] realized-distribution context consumed by review paths",
    )
    calibration_evaluate_parser.add_argument(
        "--start",
        help="evaluate only cohorts on/after this date (YYYY-MM-DD; design/confirm 分割用)",
    )
    calibration_evaluate_parser.add_argument(
        "--end",
        help="evaluate only cohorts on/before this date (YYYY-MM-DD)",
    )

    snapshot_parser = subparsers.add_parser(
        "market-snapshot",
        help="emit the market state snapshot (weekly regime history and sector aggregates)",
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

    if args.command == "prune":
        return prune_command(
            keep=args.keep,
            runs_db_path=Path(args.runs_db) if args.runs_db else None,
        )

    if args.command == "select":
        # select reads immutable run/context publications and local rule config;
        # no provider credentials are needed.
        return select_command(
            asof_date=_parse_iso_date(args.asof),
            top=args.top,
            rules=load_screening_rules(Path(args.rules_path)),
            profile=args.profile,
            detail=args.detail,
            longlist_top=args.longlist_top,
            output_path=Path(args.output_path) if args.output_path else None,
            force=args.force,
            regime_sqlite_path=Path(args.sqlite_path),
            run_revision_id=args.run_revision_id,
            runs_db_path=Path(args.runs_db) if args.runs_db else None,
            app_db_path=Path(args.app_db) if args.app_db else None,
            macro_context_id=args.macro_context_id,
            previous_run_revision_id=args.previous_run_revision_id,
            previous_shortlist_id=args.previous_shortlist_id,
            longlist_history_dir=(
                Path(args.longlist_history_dir) if args.longlist_history_dir else None
            ),
        )

    if args.command == "selection":
        # selection show reads the immutable run store only; no provider
        # credentials and no writes.
        return selection_show_command(
            selection_id=args.selection_id,
            runs_db_path=Path(args.runs_db) if args.runs_db else None,
            output_path=Path(args.output_path) if args.output_path else None,
            force=args.force,
        )

    if args.command == "shortlist" and args.shortlist_command == "outcome":
        from baibai_engine.appdb import database_path

        from .shortlist_outcome_cli import shortlist_outcome_command

        return shortlist_outcome_command(
            db_path=Path(args.db) if args.db else database_path(),
            runs_db_path=Path(args.runs_db) if args.runs_db else None,
            sqlite_path=Path(args.sqlite_path),
            horizons=args.horizons,
            output_path=Path(args.out) if args.out else None,
        )

    if args.command == "shortlist" and args.shortlist_command == "preflight":
        import sqlite3

        import yaml

        from baibai_engine.screening.shortlist_preflight import shortlist_preflight

        try:
            report = shortlist_preflight(
                as_of=_parse_iso_date(args.asof),
                cloud_summary_path=Path(args.cloud_summary),
                runs_db_path=Path(args.runs_db) if args.runs_db else RUNS_DB_PATH,
                app_db_path=Path(args.db) if args.db else APPLICATION_DB_PATH,
                repo_root=Path(args.repo_root),
                previous_run_revision_id=args.previous_run_revision_id,
            )
        except (OSError, ValueError, sqlite3.Error) as error:
            print(f"shortlist preflight failed: {error}", file=sys.stderr)
            return 1
        yaml.safe_dump(report, sys.stdout, sort_keys=False, allow_unicode=True)
        return 1 if report["decision"] == "blocked" else 0

    if args.command == "shortlist":
        from baibai_engine.screening.shortlist_cli import publish_shortlist

        return publish_shortlist(
            Path(args.draft),
            app_db_path=Path(args.db) if args.db else None,
            runs_db_path=Path(args.runs_db) if args.runs_db else None,
        )

    if args.command == "ticker-profile":
        # ticker-profile reads the SQLite stores only; no provider credentials are needed.
        return ticker_profile_command(
            ticker=args.ticker,
            asof=args.asof,
            sqlite_path=Path(args.sqlite_path),
            runs_db_path=(Path(args.runs_db) if args.runs_db else RUNS_DB_PATH),
            app_db_path=(Path(args.app_db) if args.app_db else APPLICATION_DB_PATH),
            run_revision_id=args.run_revision_id,
        )

    if args.command == "market-snapshot":
        # market-snapshot reads the SQLite store only; no provider credentials needed.
        return market_snapshot_command(
            asof=args.asof,
            weeks=args.weeks,
            sqlite_path=Path(args.sqlite_path),
        )

    if args.command == "calibration-build":
        # calibration-build reads the SQLite store and writes the local
        # calibration store only; no provider credentials are needed.
        return calibration_build_command(
            sqlite_path=Path(args.sqlite_path),
            calibration_dir=Path(args.calibration_dir),
            rules=load_screening_rules(Path(args.rules_path)),
            start=_parse_iso_date(args.start),
            end=_parse_iso_date(args.end),
            force=args.force,
            panel_variant=cast(PanelVariant, args.panel_variant),
            use_control_event_exits=not args.without_control_event_exits,
            replace_broken_current=args.replace_broken_current,
        )

    if args.command == "calibration-migrate-legacy":
        try:
            report = migrate_legacy_calibration(
                args.legacy_dir,
                args.calibration_dir,
                report_path=args.report,
            )
        except (CalibrationCacheError, OSError, ValueError) as error:
            print(f"calibration legacy migration: {error}", file=sys.stderr)
            return 1
        cohorts = cast(list[str], report["cohorts"])
        print(f"calibration legacy migration: {report['status']} ({len(cohorts)} cohorts)")
        return 0

    if args.command == "calibration-evaluate":
        return calibration_evaluate_command(
            calibration_dir=Path(args.calibration_dir),
            horizons=args.horizons,
            run_purpose=args.run_purpose,
            required_asofs=args.required_asofs,
            required_metrics=args.required_metrics,
            output_path=Path(args.out) if args.out else None,
            context_output_path=Path(args.context_out) if args.context_out else None,
            start=_parse_iso_date(args.start) if args.start else None,
            end=_parse_iso_date(args.end) if args.end else None,
        )

    if args.command == "invalidate-coverage":
        # invalidate-coverage only edits local source_coverage bookkeeping; no
        # provider credentials are needed.
        return invalidate_coverage_command(
            sqlite_path=Path(args.sqlite_path),
            source=args.source,
            start=_parse_iso_date(args.start) if args.start else None,
            end=_parse_iso_date(args.end) if args.end else None,
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
            allow_stale_snapshot=args.allow_stale_jpx if args.command == "run" else False,
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
            run_store_path=Path(args.runs_db) if args.runs_db else None,
        )

    if args.command == "bootstrap-cache":
        return bootstrap_cache_command(
            asof_date=_parse_iso_date(args.asof),
            providers=providers,
            sqlite_path=sqlite_path,
        )

    if args.command == "refresh-edinet-documents":
        return refresh_edinet_documents_command(
            asof_date=_parse_iso_date(args.asof),
            providers=providers,
        )

    if args.command == "refresh-capital-control":
        return refresh_capital_control_command(
            sqlite_path=Path(args.sqlite_path),
            asof_date=_parse_iso_date(args.asof),
        )

    if args.command == "backfill-edinet-identity":
        return backfill_edinet_identity_command(
            start=_parse_iso_date(args.start),
            end=_parse_iso_date(args.end),
            providers=providers,
        )

    if args.command == "build-control-event-exits":
        return build_control_event_exits_command(
            sqlite_path=Path(args.sqlite_path),
            asof_date=_parse_iso_date(args.asof),
            providers=providers,
        )

    if args.command == "refresh-buyback-reports":
        return refresh_buyback_reports_command(
            asof_date=_parse_iso_date(args.asof),
            lookback_days=args.lookback_days,
            providers=providers,
            sqlite_path=Path(args.sqlite_path),
        )

    if args.command == "backfill-history":
        window_start = _parse_iso_date(args.start)
        window_end = _parse_iso_date(args.end)
        if window_start > window_end:
            print(
                f"--start {window_start.isoformat()} is after --end {window_end.isoformat()}",
                file=sys.stderr,
            )
            return 1
        return backfill_history_command(
            start=window_start,
            end=window_end,
            providers=providers,
            sqlite_path=sqlite_path,
            probe_margin_publication_transition=args.probe_margin_publication_transition,
        )

    if args.command == "backfill-master":
        asof_dates = [_parse_iso_date(value) for value in (args.asofs or [])]
        if bool(args.month_end_from) != bool(args.month_end_to):
            print(
                "--month-end-from and --month-end-to must be given together",
                file=sys.stderr,
            )
            return 1
        if args.month_end_from:
            grid_start = _parse_iso_date(args.month_end_from)
            grid_end = _parse_iso_date(args.month_end_to)
            if grid_start > grid_end:
                print(
                    "--month-end-from "
                    f"{grid_start.isoformat()} is after --month-end-to {grid_end.isoformat()}",
                    file=sys.stderr,
                )
                return 1
            if not sqlite_path.exists():
                print(f"SQLite cache not found: {sqlite_path}", file=sys.stderr)
                return 1
            # The grid is derived from the bar store so that the backfilled dates
            # are the cohort dates themselves. A snapshot on any other day leaves
            # the cohort on a non-exact-date master and buys nothing.
            asof_dates.extend(month_end_asof_grid(sqlite_path, start=grid_start, end=grid_end))
        unique_dates = sorted(set(asof_dates))
        if not unique_dates:
            print("backfill-master resolved no dates to fetch", file=sys.stderr)
            return 1
        # A snapshot is only meaningful for a day the market produced one. Without
        # this check a mistyped date can persist the current population as another
        # day's point-in-time section, and every later read treats it as exact.
        today = datetime.now(JST).date()
        future = [day for day in unique_dates if day > today]
        if future:
            print(
                f"backfill-master rejects future dates: {', '.join(map(str, future))}",
                file=sys.stderr,
            )
            return 1
        traded = days_with_bars(sqlite_path, unique_dates)
        non_business = [day for day in unique_dates if day not in traded]
        if non_business:
            print(
                "backfill-master rejects dates the bar store does not show as trading "
                f"days: {', '.join(map(str, non_business))}",
                file=sys.stderr,
            )
            return 1
        return backfill_master_command(
            asof_dates=unique_dates, providers=providers, sqlite_path=sqlite_path
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
