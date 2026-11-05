"""Private original-receipt handoffs; publishers replay onto freshly hydrated stores."""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # nosec B404
import sys
import tempfile
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from baibai_engine.data_collection.providers.edinet.archives import (
    archive_keys as edinet_archive_keys,
)
from baibai_engine.data_collection.providers.jquants.dependencies import (
    export_dependencies as export_master_calendar_dependencies,
)
from baibai_engine.machine_analysis.observations.prices.calendar_context import (
    market_calendar_business_day,
)
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import (
    Boto3R2Store,
)
from baibai_engine.storage.paths import MARKET_DB_PATH
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_jobs.acquisition.economic_staging import load_handoff, save_handoff
from baibai_jobs.acquisition.edinet_archives import acquire_archives as acquire_edinet_archives
from baibai_jobs.integration.edinet_handoff import export_handoff as export_edinet_handoff
from baibai_jobs.integration.edinet_handoff import import_handoff as import_edinet_handoff
from baibai_jobs.integration.jpx_handoff import export_handoff as export_jpx_handoff
from baibai_jobs.integration.jpx_handoff import import_handoff as import_jpx_handoff
from baibai_jobs.integration.jquants_handoff import FINANCIAL_SOURCES as JQUANTS_FINANCIAL_SOURCES
from baibai_jobs.integration.jquants_handoff import MARKET_SOURCES as JQUANTS_MARKET_SOURCES
from baibai_jobs.integration.jquants_handoff import export_handoff as export_jquants_handoff
from baibai_jobs.integration.jquants_handoff import import_handoff as import_jquants_handoff
from baibai_jobs.integration.replay_result import ReplayResult
from baibai_jobs.integration.tradingview_handoff import TradingViewHandoff
from baibai_jobs.integration.tradingview_handoff import export_handoff as export_tradingview_handoff
from baibai_jobs.integration.tradingview_handoff import import_handoff as import_tradingview_handoff

SOURCES = (
    "tradingview",
    "jquants-market",
    "jquants-financial",
    "jpx-earnings",
    "jpx-regulations",
    "edinet-list",
    "edinet-metrics",
    "edinet-facts",
    "jpx-financial-pdfs",
    "jpx-capital-actions",
)
_EDINET_SOURCES = {"edinet-list", "edinet-metrics", "edinet-facts"}
_JQUANTS_SOURCES = {"jquants-market", "jquants-financial"}
_JPX_COLLECTION = {
    "jpx-earnings": "earnings_calendar",
    "jpx-regulations": "regulations",
    "jpx-financial-pdfs": "financial_statements",
    "jpx-capital-actions": "capital_actions",
}


def _retained_archive_keys(database: Path) -> set[tuple[str, int]]:
    with closing(connect_read_only(database)) as connection:
        return edinet_archive_keys(connection)


def _command(source: str, asof: date, *, database: Path) -> list[str]:
    return [
        "baibai-jobs",
        "maintain-market",
        "bootstrap-cache",
        "--source-group",
        source,
        "--asof",
        asof.isoformat(),
        "--sqlite-path",
        str(database),
    ]


def _export(
    source: str,
    asof: date,
    since: datetime,
    database: Path,
    directory: Path,
    archives: set[tuple[str, int]] | None = None,
) -> None:
    if source not in SOURCES:
        raise ValueError("unsupported market handoff source")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "receipts.sqlite"
    report: dict[str, object] = {
        "source": source,
        "asof": asof.isoformat(),
        "started_at": since.isoformat(),
    }
    if source == "tradingview":
        report.update(export_tradingview_handoff(database, path, day=asof, since=since).to_json())
    elif source in _JQUANTS_SOURCES:
        selected = (
            JQUANTS_MARKET_SOURCES if source == "jquants-market" else JQUANTS_FINANCIAL_SOURCES
        )
        report["request_ids"] = export_jquants_handoff(
            database, path, since=since, sources=selected
        )
    elif source in _JPX_COLLECTION:
        report["bundle_ids"] = export_jpx_handoff(
            database, path, since=since, collection=_JPX_COLLECTION[source]
        )
    elif source in _EDINET_SOURCES:
        report.update(
            export_edinet_handoff(database, path, since=since, archives=archives or set())
        )
    if source in {"jquants-market", "tradingview"}:
        basis = date.fromisoformat(str(report["master_asof"])) if source == "tradingview" else asof
        for dependency in export_master_calendar_dependencies(
            database, day=asof, master_asof=basis
        ):
            write_json_atomic(directory / f"dependency-{dependency['request_id']}.json", dependency)
    write_json_atomic(directory / "result.json", report)


def _handoff_ids(report: dict[str, object], field: str) -> tuple[str, ...]:
    values = report.get(field)
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError(f"invalid handoff {field}")
    return tuple(values)


def _replay(source: str, database: Path, directory: Path) -> ReplayResult:
    from baibai_jobs.acquisition.article_planning import ARTICLE_SOURCES
    from baibai_jobs.integration.article_handoff import import_handoff as import_article_handoff

    if source in ARTICLE_SOURCES:
        return import_article_handoff(source, database, directory)
    if source not in SOURCES:
        raise ValueError("unsupported market handoff source")
    report = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    if not isinstance(report, dict) or report.get("source") != source:
        raise ValueError("handoff source differs from the publication plan")
    path = directory / "receipts.sqlite"
    if source == "tradingview":
        return import_tradingview_handoff(path, database, TradingViewHandoff.from_json(report))
    if source in _JQUANTS_SOURCES:
        selected = (
            JQUANTS_MARKET_SOURCES if source == "jquants-market" else JQUANTS_FINANCIAL_SOURCES
        )
        return import_jquants_handoff(
            path, database, _handoff_ids(report, "request_ids"), sources=selected
        )
    if source in _JPX_COLLECTION:
        return import_jpx_handoff(
            path, database, _handoff_ids(report, "bundle_ids"), collection=_JPX_COLLECTION[source]
        )
    if source in _EDINET_SOURCES:
        return import_edinet_handoff(path, database)
    raise ValueError("unsupported market handoff source")


def _acquire_financial_l1(asof: date, directory: Path) -> bool:
    """Acquire from a fixed L1 release without publishing a copied L2 input."""
    from baibai_engine.data_collection.providers.jquants.http import JQuantsHTTP
    from baibai_engine.primitives.market_windows import (
        FINANCIAL_HISTORY_DAYS,
        FINANCIAL_REVISION_OVERLAP_DAYS,
    )
    from baibai_engine.storage.snapshots.objects import open_lake
    from baibai_engine.storage.snapshots.reader import resolve_current_release
    from baibai_jobs.acquisition.jquants_financial_receive import run_l1_financial_acquisition
    from baibai_jobs.publication.datasets import LAKE_DATASETS
    from baibai_jobs.publication.policy import release_policy_for_profile

    bucket = os.environ.get("R2_STORES_BUCKET") or "baibai-stores"
    from baibai_jobs.financial_cutover import financial_native_enabled

    if financial_native_enabled():
        from baibai_engine.data_collection.providers.jquants.financial_native import FinancialL1
        from baibai_jobs.acquisition.jquants_financial_native import acquire_financial_native

        with (
            closing(Boto3R2Store(bucket=bucket)) as store,
            closing(JQuantsHTTP(os.environ.get("JQUANTS_API_KEY", ""))) as client,
        ):
            result = acquire_financial_native(
                FinancialL1(store, directory),
                start=asof - timedelta(days=FINANCIAL_HISTORY_DAYS),
                end=asof,
                revision_overlap_days=FINANCIAL_REVISION_OVERLAP_DAYS,
                client=client,
            )
        return not bool(result["failed"] or result["held"])
    with (
        tempfile.TemporaryDirectory(prefix="financial-l1-") as temporary,
        open_lake(mirror=Path(temporary), bucket=bucket) as (session, cache),
        closing(Boto3R2Store(bucket=bucket)) as store,
        closing(JQuantsHTTP(os.environ.get("JQUANTS_API_KEY", ""))) as client,
    ):
        release = resolve_current_release(
            cache.source, catalog=LAKE_DATASETS, policy=release_policy_for_profile("production")
        )
        return run_l1_financial_acquisition(
            session,
            release=release,
            cache=cache,
            store=store,
            directory=directory,
            asof=asof,
            scheduled=os.environ.get("GITHUB_EVENT_NAME") == "schedule",
            lookback_days=FINANCIAL_HISTORY_DAYS,
            revision_overlap_days=FINANCIAL_REVISION_OVERLAP_DAYS,
            client=client,
        )


def _acquire_market_l1(asof: date, directory: Path) -> bool:
    """Acquire each market source using retained originals, not L2 copies."""
    from baibai_engine.data_collection.providers.jquants.http import JQuantsHTTP
    from baibai_engine.storage.snapshots.objects import open_lake
    from baibai_engine.storage.snapshots.reader import resolve_current_release
    from baibai_jobs.acquisition.jquants_market_plan import freeze_market_originals
    from baibai_jobs.acquisition.jquants_market_receive import receive_market_handoff
    from baibai_jobs.price_cutover import price_native_enabled
    from baibai_jobs.publication.datasets import LAKE_DATASETS
    from baibai_jobs.publication.policy import release_policy_for_profile

    native_prices = price_native_enabled()
    native_result: dict[str, object] | None = None
    price_ok = True
    provider_halted = False
    bucket = os.environ.get("R2_STORES_BUCKET") or "baibai-stores"
    if native_prices:
        from baibai_engine.data_collection.providers.jquants.market_rules import (
            MARKET_REVISION_OVERLAP_DAYS,
        )
        from baibai_engine.data_collection.providers.jquants.price_native import PriceL1
        from baibai_engine.primitives.market_windows import FINANCIAL_HISTORY_DAYS
        from baibai_jobs.acquisition.jquants_price_native import acquire_price_native

        # Commit price originals first. A missing legacy release or another market
        # source's later failure cannot roll them back or prevent this acquisition.
        try:
            with (
                closing(Boto3R2Store(bucket=bucket)) as price_store,
                closing(JQuantsHTTP(os.environ.get("JQUANTS_API_KEY", ""))) as price_client,
            ):
                result = acquire_price_native(
                    PriceL1(price_store, directory / "price-native"),
                    start=asof - timedelta(days=FINANCIAL_HISTORY_DAYS),
                    end=asof,
                    revision_overlap_days=MARKET_REVISION_OVERLAP_DAYS,
                    client=price_client,
                )
            provider_halted = bool(result.get("halted", 0))
            native_result = dict(result)
            price_ok = not bool(result["failed"] or result["held"])
        except Exception as error:
            price_ok = False
            native_result = {"error": type(error).__name__}
        directory.mkdir(parents=True, exist_ok=True)
        write_json_atomic(directory / "price-native-result.json", native_result)
    with (
        tempfile.TemporaryDirectory(prefix="market-l1-") as temporary,
        open_lake(mirror=Path(temporary), bucket=bucket) as (session, cache),
        closing(Boto3R2Store(bucket=bucket)) as store,
        closing(JQuantsHTTP(os.environ.get("JQUANTS_API_KEY", ""))) as client,
    ):
        release = resolve_current_release(
            cache.source, catalog=LAKE_DATASETS, policy=release_policy_for_profile("production")
        )
        fixed = freeze_market_originals(
            session,
            release=release,
            cache=cache,
            store=store,
            asof=asof,
            include_daily_prices=not native_prices,
        )
        legacy_ok = receive_market_handoff(
            directory,
            fixed,
            asof=asof,
            client=client,
            scheduled=os.environ.get("GITHUB_EVENT_NAME") == "schedule",
            include_daily_prices=not native_prices,
            provider_halted=provider_halted,
        )

    if native_result is not None:
        path = directory / "result.json"
        report = json.loads(path.read_bytes())
        report["independent_l1"] = {"daily_bars": native_result}
        write_json_atomic(path, report)
    return legacy_ok and price_ok


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("input", "resume", "acquire", "export", "stage", "download", "replay-copy"),
    )
    parser.add_argument("--source", choices=SOURCES, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--database", type=Path, default=MARKET_DB_PATH)
    parser.add_argument("--since", type=datetime.fromisoformat)
    parser.add_argument("--asof", type=date.fromisoformat)
    parser.add_argument("--stage-id")
    parser.add_argument("--input-directory", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    directory = args.directory
    try:
        if args.command == "replay-copy":
            replay = _replay(args.source, args.database, directory)
            print(json.dumps(replay.report, sort_keys=True))
            return int(not replay.complete)
        if args.command == "input":
            from baibai_jobs.acquisition.source_inputs import load_input

            load_input(
                Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores"),
                args.source,
                args.database,
                directory,
            )
        elif args.command == "resume":
            from baibai_jobs.acquisition.receipts import available_handoffs, download_fixed

            if args.asof is None:
                raise ValueError("--asof is required")
            store = Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
            saved_input = json.loads((directory / "result.json").read_text())
            sources = (
                ("edinet-list", args.source)
                if args.source in {"edinet-metrics", "edinet-facts"}
                else (args.source,)
            )
            for source in sources:
                for index, entry in enumerate(
                    available_handoffs(store, source=source, include_integrated=True)
                ):
                    if entry.get("invalid") or entry.get("late_before_integrated"):
                        continue
                    integrated = entry.get("integration")
                    if isinstance(integrated, dict) and (
                        integrated["status"] == "needs_explicit_recovery"
                        or integrated["integrated_at"] <= saved_input["created_at"]
                    ):
                        continue
                    if args.source == "tradingview" and entry["asof"] != args.asof.isoformat():
                        continue
                    if (
                        args.source.startswith("edinet-")
                        and str(entry["asof"]) > args.asof.isoformat()
                    ):
                        continue
                    packet = directory / f"resume-{source}-{index}"
                    download_fixed(store, entry, packet)
                    _replay(source, args.database, packet)
        elif args.command in {"stage", "download"}:
            if args.stage_id is None:
                raise ValueError("--stage-id is required")
            store = Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
            if args.command == "stage":
                save_handoff(store, args.stage_id, directory, market=True)
                if output := os.environ.get("GITHUB_OUTPUT"):
                    with Path(output).open("a", encoding="utf-8") as stream:
                        print(f"handoff={args.stage_id}", file=stream)
            else:
                load_handoff(store, args.stage_id, directory, market=True)
        elif args.command in {"acquire", "export"}:
            if args.asof is None or (args.command == "export" and args.since is None):
                raise ValueError("--asof and export --since are required")
            if args.command == "acquire" and args.source in {"jquants-financial", "jquants-market"}:
                acquire = (
                    _acquire_financial_l1
                    if args.source == "jquants-financial"
                    else _acquire_market_l1
                )
                ok = acquire(args.asof, directory)
                if output := os.environ.get("GITHUB_OUTPUT"):
                    with Path(output).open("a", encoding="utf-8") as stream:
                        from baibai_jobs.financial_cutover import financial_native_enabled

                        native = args.source == "jquants-financial" and financial_native_enabled()
                        print("stage_ready=" + str(not native).lower(), file=stream)
                        if native:
                            print("native_l1=true", file=stream)
                return int(not ok)
            since = args.since or datetime.now(UTC)
            before = (
                _retained_archive_keys(args.database)
                if args.source.startswith("edinet-")
                else set()
            )
            status = 0
            acquisition_report: dict[str, object] = {}
            if args.command == "acquire":
                if args.source in {"tradingview", "jpx-financial-pdfs", "jpx-capital-actions"}:
                    raise ValueError(
                        "Use the source's explicit original receipt import; no live client here"
                    )
                if (
                    args.source.startswith("jquants-")
                    and os.environ.get("GITHUB_EVENT_NAME") == "schedule"
                    and market_calendar_business_day(args.database, args.asof) is False
                ):
                    acquisition_report = {"status": "skipped_non_business_day"}
                elif args.source in {"edinet-metrics", "edinet-facts"}:
                    if args.input_directory is None:
                        raise ValueError(
                            "EDINET archive acquisition requires its scoped input manifest"
                        )
                    saved = json.loads((args.input_directory / "result.json").read_text())
                    result = acquire_edinet_archives(
                        args.database,
                        group=args.source,
                        asof=args.asof,
                        api_key=os.environ.get("EDINET_API_KEY", ""),
                        retained={(str(key[0]), int(key[1])) for key in saved["archive_keys"]}
                        | before,
                    )
                    print(json.dumps(result, sort_keys=True))
                    acquisition_report = result
                    status = int(result["status"] == "failed")
                else:
                    status = subprocess.run(
                        _command(args.source, args.asof, database=args.database), check=False
                    ).returncode  # nosec B603
            archives = (
                _retained_archive_keys(args.database) - before
                if args.source.startswith("edinet-")
                else set()
            )
            _export(args.source, args.asof, since, args.database, directory, archives)
            report = json.loads((directory / "result.json").read_text())
            report["acquisition"] = acquisition_report or {
                "status": "failed" if status else "received"
            }
            if args.input_directory is not None:
                report["input_generation"] = json.loads(
                    (args.input_directory / "result.json").read_text()
                )["canonical_generations"]
            write_json_atomic(directory / "result.json", report)
            if output := os.environ.get("GITHUB_OUTPUT"):
                with Path(output).open("a", encoding="utf-8") as stream:
                    print("stage_ready=true", file=stream)
            return status
    except Exception as error:
        # Raw provider payload and credentials must not enter cross-job output.
        print(f"source handoff failed: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
