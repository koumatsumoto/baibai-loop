"""source取得・保存原本の投影・保守の合成。L1の通信実装ではない。"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

from baibai_engine.primitives.dates import parse_iso_date as _parse_iso_date
from baibai_engine.foundation.env import load_project_env
from baibai_engine.primitives.time import JST
from baibai_engine.market.calendar_context import market_calendar_business_day
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
from baibai_engine.market.jpx_rebuild import rebuild_jpx_snapshot
from baibai_engine.market.jpx_refresh import JPXRefresh
from baibai_engine.market.jquants_refresh import JQuantsRefresh
from baibai_engine.market.valuation_catalysts import (
    TSECapitalPolicyRefreshSummary,
    ValuationCatalystError,
)
from baibai_engine.storage.observations.schema import SQLiteSchemaError

from .cache import (
    backfill_history_command,
    backfill_master_command,
    bootstrap_cache_command,
    invalidate_coverage_command,
    refresh_edinet_documents_command,
)
from .config import DEFAULT_SQLITE_CACHE_DIR, ConfigError, SourceMaintenanceConfig
from .delisting_cli import (
    build_tender_offer_exits_command,
    refresh_jpx_delistings_command,
    refresh_tender_offer_exits_command,
)
from .providers import ProviderBundle
from .valuation_catalyst_cli import (
    backfill_edinet_identity_command,
    refresh_tse_capital_policy_command,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine data-collection market")
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap_parser = subparsers.add_parser(
        "bootstrap-cache",
        help="bootstrap canonical SQLite inputs for a source as-of date",
    )
    bootstrap_parser.add_argument(
        "--asof",
        required=True,
        help="source target date (YYYY-MM-DD); computes each source window automatically",
    )

    bootstrap_parser.add_argument("--sqlite-path", help="scoped acquisition store path")
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
        metrics_parser.add_argument("--sqlite-path", help="scoped extraction store path")
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

    if args.command == "invalidate-coverage":
        # invalidate-coverage only edits local source_coverage bookkeeping; no
        # provider credentials are needed.
        return invalidate_coverage_command(
            sqlite_path=Path(args.sqlite_path),
            source=args.source,
            start=_parse_iso_date(args.start) if args.start else None,
            end=_parse_iso_date(args.end) if args.end else None,
        )

    if args.command == "refresh-tse-capital-policy":
        return refresh_tse_capital_policy_command(
            sqlite_path=Path(args.sqlite_path),
        )

    if args.command == "refresh-jpx-delistings":
        return refresh_jpx_delistings_command(sqlite_path=Path(args.sqlite_path))

    try:
        config = SourceMaintenanceConfig.from_env()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    sqlite_path = config.sqlite_cache_dir / "market.sqlite"

    if (
        args.command in {"bootstrap-cache", "extract-edinet-metrics", "refresh-edinet-metrics"}
        and args.sqlite_path
    ):
        sqlite_path = Path(args.sqlite_path)

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
            jquants_refresh=jquants_refresh,
            edinet_refresh=edinet_refresh,
            jpx_refresh=jpx_refresh,
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
            from baibai_engine.machine_analysis.historical_evaluation.grid import (
                days_with_bars,
                month_end_asof_grid,
            )

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
