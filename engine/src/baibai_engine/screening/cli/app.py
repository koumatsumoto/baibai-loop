"""Argument parser and dispatch for the screening CLI."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import cast

from baibai_engine.foundation.env import load_project_env
from baibai_engine.foundation.repository_layout import APPLICATION_DB_PATH, RUNS_DB_PATH
from baibai_engine.foundation.time import JST
from baibai_engine.market.delistings import DelistingSourceError
from baibai_engine.market.edinet_facts.service import (
    extract_edinet_facts_command,
    refresh_edinet_facts_command,
)
from baibai_engine.market.edinet_list_rebuild import rebuild_edinet_lists_command
from baibai_engine.market.edinet_metrics.service import (
    extract_edinet_metrics_command,
    refresh_edinet_metrics_command,
)
from baibai_engine.market.edinet_reader import EDINETReader
from baibai_engine.market.edinet_refresh import EDINETRefresh
from baibai_engine.market.jpx_models import (
    JPXEarningsCalendarSnapshot,
    JPXProviderError,
    JPXRegulationSnapshot,
)
from baibai_engine.market.jpx_reader import JPXReader
from baibai_engine.market.jpx_rebuild import rebuild_jpx_snapshot
from baibai_engine.market.jpx_refresh import JPXRefresh
from baibai_engine.market.jquants_reader import JQuantsReader
from baibai_engine.market.jquants_refresh import JQuantsRefresh
from baibai_engine.market.valuation_catalysts import (
    TSECapitalPolicyRefreshSummary,
    ValuationCatalystError,
)
from baibai_engine.read_api.market import market_calendar_business_day
from baibai_engine.screening.calibration.cli import (
    calibration_build_command,
    calibration_evaluate_command,
)
from baibai_engine.screening.calibration.evidence import DECISION_SUBJECTS
from baibai_engine.screening.calibration.grid import days_with_bars, month_end_asof_grid
from baibai_engine.screening.calibration.panel import PANEL_BUILD_POLICIES, PanelVariant
from baibai_engine.screening.calibration.store import (
    DEFAULT_CALIBRATION_DIR,
)
from baibai_engine.screening.config import (
    DEFAULT_SQLITE_CACHE_DIR,
    ConfigError,
    ScreeningConfig,
)
from baibai_engine.screening.rule_config import (
    DEFAULT_RULES_PATH,
    load_screening_rules,
)
from baibai_engine.screening.sqlite_coverage import (
    verify_screening_sqlite_coverage,
)
from baibai_engine.storage.observations.schema import SQLiteSchemaError

from .cache import (
    _print_cache_coverage_issues,
    backfill_history_command,
    backfill_master_command,
    bootstrap_cache_command,
    invalidate_coverage_command,
    refresh_edinet_documents_command,
    verify_cache_coverage_command,
)
from .common import _parse_iso_date
from .delisting_cli import (
    build_tender_offer_exits_command,
    refresh_jpx_delistings_command,
    refresh_tender_offer_exits_command,
)
from .providers import ProviderBundle
from .prune import prune_command
from .query import (
    candidates_show_command,
    market_snapshot_command,
    run_show_command,
    ticker_profile_command,
)
from .run import run_command
from .valuation_catalyst_cli import (
    backfill_edinet_identity_command,
    refresh_tse_capital_policy_command,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine screening")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="produce a Screening Run")
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
        "--run-revision-id",
        help="preallocated immutable revision for idempotent machine resume",
    )
    run_parser.add_argument(
        "--run-at",
        type=datetime.fromisoformat,
        help="fixed timezone-aware publication clock for idempotent machine resume",
    )
    run_parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing Screening Run YAML output path",
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

    bootstrap_parser.add_argument(
        "--source-group",
        default="all",
        choices=(
            "all",
            "jquants-market",
            "jquants-financial",
            "edinet-list",
            "jpx-earnings",
            "jpx-regulations",
        ),
        help="acquire only one independent source group",
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

    rebuild_lists_parser = subparsers.add_parser(
        "rebuild-edinet-lists",
        help="rebuild EDINET document-list L2 from retained raw receipts without acquisition",
    )
    rebuild_lists_parser.add_argument("--start", required=True, help="first date (YYYY-MM-DD)")
    rebuild_lists_parser.add_argument("--end", required=True, help="last date (YYYY-MM-DD)")
    rebuild_lists_parser.add_argument(
        "--sqlite-path",
        default=str(DEFAULT_SQLITE_CACHE_DIR / "market.sqlite"),
        help=f"SQLite cache path (default: {DEFAULT_SQLITE_CACHE_DIR}/market.sqlite)",
    )

    rebuild_jpx_parser = subparsers.add_parser(
        "rebuild-jpx-snapshot",
        help="rebuild one JPX snapshot from a selected complete source bundle without acquisition",
    )
    rebuild_jpx_parser.add_argument("--bundle-id", required=True)
    rebuild_jpx_parser.add_argument("--sqlite-path", required=True)

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
        help="add each month's last qualifying observed date through --month-end-to (YYYY-MM-DD)",
    )
    backfill_master_parser.add_argument(
        "--month-end-to",
        help="last date considered for --month-end-from (YYYY-MM-DD)",
    )

    extract_parser = subparsers.add_parser(
        "extract-edinet-metrics",
        help="recompute EDINET type=5 CSV metrics from stored documents and archives",
    )
    refresh_metrics_parser = subparsers.add_parser(
        "refresh-edinet-metrics",
        help="acquire EDINET document lists and needed CSV archives, then extract metrics",
    )
    for metrics_parser in (extract_parser, refresh_metrics_parser):
        metrics_parser.add_argument(
            "--asof",
            required=True,
            help="metrics as-of date (YYYY-MM-DD), not a point-in-time EDINET reconstruction",
        )
        metrics_parser.add_argument(
            "--lookback-days",
            type=int,
            default=540,
            help="EDINET document-list lookback window in calendar days (default 540)",
        )

    facts_parser = subparsers.add_parser(
        "extract-edinet-facts", help="extract annual Research facts from stored XBRL archives"
    )
    refresh_facts_parser = subparsers.add_parser(
        "refresh-edinet-facts", help="acquire XBRL archives and extract annual Research facts"
    )
    for facts_command_parser in (facts_parser, refresh_facts_parser):
        facts_command_parser.add_argument("--asof", required=True)
        facts_command_parser.add_argument(
            "--initialize",
            action="store_true",
            help="explicit initial processing of the latest annual families",
        )
        facts_command_parser.add_argument(
            "--ticker",
            action="append",
            default=[],
            help="restrict local evaluation; does not initialize the global baseline",
        )
        facts_command_parser.add_argument(
            "--sqlite-path", help="staged market store for local initialization"
        )

    capital_policy_parser = subparsers.add_parser(
        "refresh-tse-capital-policy",
        help="re-read the TSE capital-policy disclosure workbook",
    )
    capital_policy_parser.add_argument(
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

    delistings_parser = subparsers.add_parser(
        "refresh-jpx-delistings",
        help="capture the linked JPX delisting pages and replace the observed view",
    )
    delistings_parser.add_argument(
        "--sqlite-path",
        default=str(DEFAULT_SQLITE_CACHE_DIR / "market.sqlite"),
        help=f"SQLite cache path (default: {DEFAULT_SQLITE_CACHE_DIR}/market.sqlite)",
    )

    for name, help_text in (
        ("build-tender-offer-exits", "derive tender-offer exits from stored EDINET archives"),
        ("refresh-tender-offer-exits", "acquire EDINET archives and derive tender-offer exits"),
    ):
        exits_parser = subparsers.add_parser(name, help=help_text)
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
        help="invalidate fetch coverage without deleting stored observations",
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

    run_show = subparsers.add_parser("run-show", help="export one saved Stock Analysis run")
    candidates = subparsers.add_parser("candidates", help="read candidates from a saved run")
    candidates_commands = candidates.add_subparsers(dest="candidates_command", required=True)
    candidates_show = candidates_commands.add_parser("show", help="show one run's candidates")
    for show in (run_show, candidates_show):
        show.add_argument("--run-revision-id", required=True)
        show.add_argument("--runs-db", help="screening run store path")
        show.add_argument("--output-path", help="also write the same output to this path")
        show.add_argument("--force", action="store_true", help="overwrite an existing output file")
        show.add_argument("--format", choices=("yaml", "json"), default="yaml")

    profile_parser = subparsers.add_parser(
        "ticker-profile",
        help=(
            "emit the single-ticker deterministic fact profile "
            "(price, relative, benchmark trend, filings, screening)"
        ),
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
        "--without-control-event-exits",
        action="store_true",
        help=(
            "resolve forward windows from market closes alone, leaving delisted names to "
            "sensitivity scenarios; regenerates the comparison baseline"
        ),
    )
    calibration_build_parser.add_argument(
        "--without-failure-exits",
        action="store_true",
        help=(
            "leave windows ended by a failure delisting unresolved instead of pricing them "
            "at the last close; regenerates the comparison baseline"
        ),
    )

    calibration_evaluate_parser = subparsers.add_parser(
        "calibration-evaluate",
        help="evaluate stored calibration cohorts (rank IC / decile / candidate replay)",
    )
    calibration_evaluate_parser.add_argument(
        "--calibration-dir",
        default=str(DEFAULT_CALIBRATION_DIR),
        help=f"panel / forward store directory (default: {DEFAULT_CALIBRATION_DIR})",
    )
    calibration_evaluate_parser.add_argument(
        "--rules-path",
        default=os.environ.get("SCREENING_RULES_PATH") or str(DEFAULT_RULES_PATH),
        help=f"screening rules path (default: SCREENING_RULES_PATH or {DEFAULT_RULES_PATH})",
    )
    calibration_evaluate_parser.add_argument(
        "--horizon",
        action="append",
        dest="horizons",
        help="horizon to evaluate (3m/6m/1y/3y/5y; repeatable; default: all)",
    )
    calibration_evaluate_parser.add_argument(
        "--run-purpose",
        choices=("diagnostic", "empirical_change_evidence"),
        default="diagnostic",
    )
    calibration_evaluate_parser.add_argument(
        "--required-asof", action="append", dest="required_asofs"
    )
    calibration_evaluate_parser.add_argument(
        "--required-metric", action="append", dest="required_metrics"
    )
    calibration_evaluate_parser.add_argument(
        "--decision-subject",
        choices=DECISION_SUBJECTS,
        default="estimator_policy",
        help="evidence subject whose mandatory metrics cannot be omitted",
    )
    calibration_evaluate_parser.add_argument(
        "--candidate-discovery-approach",
        help="one valuation approach when decision subject is candidate_discovery_approach",
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
        help="emit the market snapshot (weekly benchmark trend and sector aggregates)",
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
    snapshot_parser.add_argument(
        "--format",
        choices=("yaml", "json"),
        default="yaml",
        help="output format (default: yaml)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "rebuild-jpx-snapshot":
        try:
            snapshot = rebuild_jpx_snapshot(Path(args.sqlite_path), args.bundle_id)
        except (
            JPXProviderError,
            DelistingSourceError,
            ValuationCatalystError,
            SQLiteSchemaError,
            sqlite3.Error,
            ValueError,
        ) as exc:
            print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        if isinstance(snapshot, JPXRegulationSnapshot):
            collection = "regulations"
            rows = len(snapshot.flags_by_ticker)
        elif isinstance(snapshot, TSECapitalPolicyRefreshSummary):
            collection = "capital_policy"
            rows = snapshot.row_count
        elif isinstance(snapshot, tuple):
            collection = "delistings"
            rows = len(snapshot)
        else:
            assert isinstance(snapshot, JPXEarningsCalendarSnapshot)
            collection = "earnings_calendar"
            rows = snapshot.valid_record_count
        print(
            f"rebuild-jpx-snapshot: collection={collection} bundle_id={args.bundle_id} rows={rows}"
        )
        return 0

    load_project_env()

    if args.command == "rebuild-edinet-lists":
        return rebuild_edinet_lists_command(
            sqlite_path=Path(args.sqlite_path),
            start=_parse_iso_date(args.start),
            end=_parse_iso_date(args.end),
        )

    if args.command == "prune":
        return prune_command(
            keep=args.keep,
            runs_db_path=Path(args.runs_db) if args.runs_db else None,
        )

    if args.command in {"run-show", "candidates"}:
        command = candidates_show_command if args.command == "candidates" else run_show_command
        return command(
            run_revision_id=args.run_revision_id,
            runs_db_path=Path(args.runs_db) if args.runs_db else None,
            output_path=Path(args.output_path) if args.output_path else None,
            force=args.force,
            output_format=args.format,
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
            output_format=args.format,
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
            use_failure_exits=not args.without_failure_exits,
        )

    if args.command == "calibration-evaluate":
        return calibration_evaluate_command(
            calibration_dir=Path(args.calibration_dir),
            rules=load_screening_rules(Path(args.rules_path)),
            horizons=args.horizons,
            run_purpose=args.run_purpose,
            required_asofs=args.required_asofs,
            required_metrics=args.required_metrics,
            decision_subject=args.decision_subject,
            candidate_discovery_approach=args.candidate_discovery_approach,
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

    if args.command == "refresh-tse-capital-policy":
        return refresh_tse_capital_policy_command(
            sqlite_path=Path(args.sqlite_path),
        )

    if args.command == "refresh-jpx-delistings":
        return refresh_jpx_delistings_command(sqlite_path=Path(args.sqlite_path))

    try:
        config = ScreeningConfig.from_env()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    sqlite_path = config.sqlite_cache_dir / "market.sqlite"
    if args.command == "build-tender-offer-exits":
        exits_path = Path(args.sqlite_path)
        return build_tender_offer_exits_command(
            sqlite_path=exits_path,
            asof_date=_parse_iso_date(args.asof),
            reader=EDINETReader(exits_path),
        )
    if args.command == "refresh-tender-offer-exits":
        exits_path = Path(args.sqlite_path)
        with ExitStack() as resources:
            exits_edinet = EDINETRefresh(
                config.edinet_api_key,
                sqlite_path=exits_path,
            )
            resources.callback(exits_edinet.close)
            return refresh_tender_offer_exits_command(
                sqlite_path=exits_path,
                asof_date=_parse_iso_date(args.asof),
                reader=EDINETReader(exits_path),
                acquisition=exits_edinet,
            )
    if args.command == "extract-edinet-metrics":
        if args.lookback_days < 0:
            print("--lookback-days must be zero or greater", file=sys.stderr)
            return 1
        return extract_edinet_metrics_command(
            asof_date=_parse_iso_date(args.asof),
            lookback_days=args.lookback_days,
            reader=EDINETReader(sqlite_path),
            sqlite_path=sqlite_path,
        )
    if args.command == "extract-edinet-facts":
        facts_path = Path(args.sqlite_path) if args.sqlite_path else sqlite_path
        return extract_edinet_facts_command(
            asof=_parse_iso_date(args.asof),
            sqlite_path=facts_path,
            reader=EDINETReader(facts_path),
            initialize=args.initialize,
            tickers=tuple(args.ticker),
        )
    if args.command == "refresh-edinet-facts":
        facts_path = Path(args.sqlite_path) if args.sqlite_path else sqlite_path
        with ExitStack() as resources:
            facts_edinet = EDINETRefresh(
                config.edinet_api_key,
                sqlite_path=facts_path,
            )
            resources.callback(facts_edinet.close)
            return refresh_edinet_facts_command(
                asof=_parse_iso_date(args.asof),
                sqlite_path=facts_path,
                reader=EDINETReader(facts_path),
                acquisition=facts_edinet,
                initialize=args.initialize,
                tickers=tuple(args.ticker),
            )
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
    with ExitStack() as resources:
        jquants_refresh = (
            JQuantsRefresh(sqlite_path, config.jquants_api_key)
            if args.command in {"backfill-history", "backfill-master"}
            or (
                args.command == "bootstrap-cache"
                and args.source_group in {"all", "jquants-market", "jquants-financial"}
            )
            else None
        )
        if jquants_refresh is not None:
            resources.callback(jquants_refresh.close)
        edinet_refresh = (
            EDINETRefresh(
                config.edinet_api_key,
                sqlite_path=sqlite_path,
            )
            if config.edinet_api_key
            and (args.command != "bootstrap-cache" or args.source_group in {"all", "edinet-list"})
            and args.command
            in {
                "bootstrap-cache",
                "refresh-edinet-documents",
                "backfill-edinet-identity",
                "refresh-edinet-metrics",
            }
            else None
        )
        if edinet_refresh is not None:
            resources.callback(edinet_refresh.close)
        jpx_refresh = (
            JPXRefresh(sqlite_path=sqlite_path)
            if args.command == "bootstrap-cache"
            and args.source_group in {"all", "jpx-earnings", "jpx-regulations"}
            else None
        )
        if jpx_refresh is not None:
            resources.callback(jpx_refresh.close)
        edinet_reader = EDINETReader(sqlite_path)
        providers = ProviderBundle(
            jquants=JQuantsReader(sqlite_path),
            edinet=edinet_reader,
            jpx=JPXReader(
                sqlite_path,
                allow_stale_snapshot=args.allow_stale_jpx if args.command == "run" else False,
            ),
            jquants_refresh=jquants_refresh,
            edinet_refresh=edinet_refresh,
            jpx_refresh=jpx_refresh,
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
                now=args.run_at,
                output_path=Path(args.output_path) if args.output_path else None,
                force=args.force,
                run_store_path=Path(args.runs_db) if args.runs_db else None,
                run_revision_id=args.run_revision_id,
            )

        if args.command == "bootstrap-cache":
            return bootstrap_cache_command(
                asof_date=_parse_iso_date(args.asof),
                providers=providers,
                sqlite_path=sqlite_path,
                source_group=args.source_group,
            )

        if args.command == "refresh-edinet-documents":
            return refresh_edinet_documents_command(
                asof_date=_parse_iso_date(args.asof),
                providers=providers,
            )

        if args.command == "backfill-edinet-identity":
            return backfill_edinet_identity_command(
                start=_parse_iso_date(args.start),
                end=_parse_iso_date(args.end),
                providers=providers,
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
            historical = [day for day in unique_dates if day < today]
            traded = days_with_bars(sqlite_path, historical)
            non_business = [day for day in historical if day not in traded]
            if non_business:
                print(
                    "backfill-master rejects dates the bar store does not show as trading "
                    f"days: {', '.join(map(str, non_business))}",
                    file=sys.stderr,
                )
                return 1
            if today in unique_dates:
                try:
                    business = market_calendar_business_day(sqlite_path, today)
                except sqlite3.Error:
                    print("backfill-master: market calendar unavailable", file=sys.stderr)
                    return 1
                if business is None:
                    print("backfill-master: market calendar coverage unavailable", file=sys.stderr)
                    return 1
                if not business:
                    print(f"backfill-master {today}: non-business skip")
                    unique_dates.remove(today)
            if not unique_dates:
                return 0
            return backfill_master_command(
                asof_dates=unique_dates, providers=providers, sqlite_path=sqlite_path
            )

        if args.command == "refresh-edinet-metrics":
            if args.lookback_days < 0:
                print("--lookback-days must be zero or greater", file=sys.stderr)
                return 1
            if providers.edinet_refresh is None:
                print("EDINET_API_KEY is required for refresh-edinet-metrics", file=sys.stderr)
                return 1
            return refresh_edinet_metrics_command(
                asof_date=_parse_iso_date(args.asof),
                lookback_days=args.lookback_days,
                reader=edinet_reader,
                acquisition=providers.edinet_refresh,
                sqlite_path=sqlite_path,
            )

        raise AssertionError(f"unreachable command: {args.command!r}")
