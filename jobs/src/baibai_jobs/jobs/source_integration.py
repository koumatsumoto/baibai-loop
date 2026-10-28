"""One scheduled canonical integration, independent of acquisition completion."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import date
from pathlib import Path

from baibai_engine.data_collection.economic.store import DEFAULT_DB_PATH as DEFAULT_ECONOMIC_DB_PATH
from baibai_engine.storage.paths import MARKET_DB_PATH
from baibai_engine.data_collection.economic.store import EconomicSchemaError
from baibai_engine.market.jquants_decode import JQuantsProviderError
from baibai_engine.storage.observations.schema import SQLiteSchemaError as MarketSchemaError
from baibai_engine.machine_analysis.observations.external_assessments.tradingview.observations import FetchError as TradingViewFetchError
from baibai_engine.data_collection.economic.handoff import accept_rechecks as accept_economic_rechecks
from baibai_engine.market.edinet_integration import extract_saved_edinet
from baibai_engine.data_collection.economic.store import validate_current_schema as validate_economic_schema
from baibai_engine.storage.observations.schema import validate_current_schema as validate_market_schema
from baibai_jobs.jobs.external_assessment_integration import merge_ready, verify_and_seal
from baibai_jobs.jobs.source_handoff import _replay, complete
from baibai_jobs.storage.external_assessment_ready import available_ready
from baibai_jobs.storage.json_io import write_json_atomic
from baibai_jobs.storage.lake_publish import Boto3R2Store, LakePublishError
from baibai_jobs.storage.merge_economic_store import merge_stores, verify_stores
from baibai_jobs.storage.publication_lease import _load_handle, verify_held
from baibai_jobs.storage.publish_market_lake import _serving_pointer
from baibai_jobs.storage.source_inputs import publish_inputs
from baibai_jobs.storage.source_receipts import available_handoffs, download_fixed, seal_integrated
from baibai_jobs.storage.store_merge import MergeError


def apply(
    store: Boto3R2Store,
    directory: Path,
    *,
    market: Path,
    economic: Path,
    asof: date,
) -> dict[str, object]:
    plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
    # Canonical corruption is global and fatal, before any handoff is applied.
    for database, validate in (
        (market, validate_market_schema),
        (economic, validate_economic_schema),
    ):
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            validate(conn)
    outcomes = []
    for index, entry in enumerate(plan["handoffs"]):
        packet = directory / f"receipt-{index:05d}"
        result: dict[str, object] = {}
        applied, failed, recovery = False, False, False
        if entry.get("invalid") or entry.get("late_before_integrated"):
            result = {
                "reason": "invalid_handoff"
                if entry.get("invalid")
                else "older_than_integrated_source_watermark"
            }
            failed = recovery = True
        else:
            try:
                download_fixed(store, entry, packet)
                # Invalid source bytes are local failures. SQLite errors from the
                # canonical writers below remain fatal storage/integrity failures.
                try:
                    with closing(
                        sqlite3.connect(
                            (packet / "receipts.sqlite").resolve().as_uri() + "?mode=ro", uri=True
                        )
                    ) as raw:
                        (validate_market_schema if entry["market"] else validate_economic_schema)(
                            raw
                        )
                except sqlite3.DatabaseError as error:
                    raise ValueError("invalid source SQLite receipt") from error
                if entry["market"]:
                    result = _replay(entry["source"], market, packet, extract=False)
                    failed = recovery = not complete(result)
                else:
                    accepted = packet / "accepted.sqlite"
                    result = accept_economic_rechecks(
                        packet / "receipts.sqlite", economic, accepted
                    )
                    merge_stores(accepted, economic)
                    failed = recovery = bool(result["late_recovery_required"])
                applied = True
            except LakePublishError as error:
                if str(error).startswith("R2 "):
                    raise
                result = {"reason": type(error).__name__}
                failed = recovery = True
            except (
                ValueError,
                MergeError,
                MarketSchemaError,
                EconomicSchemaError,
                JQuantsProviderError,
                TradingViewFetchError,
                sqlite3.IntegrityError,
            ) as error:
                # Each owner keeps failed projections unchanged/atomic. Originals
                # already retained by an owner remain durable and are published.
                result = {"reason": type(error).__name__}
                failed = recovery = True
        outcomes.append(
            {
                "entry": entry,
                "result": result,
                "applied": applied,
                "failed": failed,
                "recovery": recovery,
            }
        )
    external = (
        merge_ready(store, plan["external"], database=market) if plan.get("external") else None
    )
    extractions = extract_saved_edinet(market, asof=asof)
    report: dict[str, object] = {
        "asof": asof.isoformat(),
        "handoffs": outcomes,
        "extractions": extractions,
        "external": external,
        "external_failed": plan.get("external_failed", []),
    }
    write_json_atomic(directory / "applied.json", report)
    return report


def canonical_generations() -> dict[str, str]:
    stores = Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
    pointer = _serving_pointer(stores).pointer
    if pointer is None:
        raise ValueError("common integration requires a published lake release")
    generation = (
        Path(os.environ.get("R2_GENERATION_DIR") or "stores/.r2-generations")
        / "indicators.sqlite.etag"
    )
    return {
        "lake_release_id": pointer.release_id,
        "lake_manifest_sha256": pointer.manifest_sha256,
        "economic_etag": generation.read_text(encoding="utf-8").strip(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "apply", "verify", "seal", "inputs", "check"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--market", type=Path, default=MARKET_DB_PATH)
    parser.add_argument("--economic", type=Path, default=DEFAULT_ECONOMIC_DB_PATH)
    parser.add_argument("--asof", type=date.fromisoformat)
    parser.add_argument("--identity")
    parser.add_argument("--lease-handle", type=Path)
    args = parser.parse_args(argv)
    try:
        directory = args.directory
        if args.command == "check":
            report = json.loads((directory / "applied.json").read_text())
            integration_failed = (
                any(item["failed"] for item in report["handoffs"])
                or any(item["exit_code"] for item in report["extractions"].values())
                or bool(report.get("external_failed"))
                or bool((report.get("external") or {}).get("failed"))
            )
            deferred = {
                source: item
                for source, item in report["extractions"].items()
                if item.get("deferred_inputs")
            }
            print(
                json.dumps(
                    {
                        "integration_status": "failed"
                        if integration_failed
                        else "completed_with_deferred_inputs"
                        if deferred
                        else "completed",
                        "deferred_inputs": deferred,
                    },
                    sort_keys=True,
                )
            )
            return int(integration_failed)
        if args.command == "verify":
            report = json.loads((directory / "applied.json").read_text(encoding="utf-8"))
            for index, outcome in enumerate(report["handoffs"]):
                if not outcome["entry"]["market"] and outcome["applied"]:
                    verify_stores(
                        directory / f"receipt-{index:05d}" / "accepted.sqlite", args.economic
                    )
            return 0
        store = Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
        directory.mkdir(parents=True, exist_ok=True)
        if args.command == "plan":
            if (directory / "plan.json").exists():
                raise FileExistsError("integration inputs are already fixed")
            external, failed = available_ready(store)
            write_json_atomic(
                directory / "plan.json",
                {
                    "handoffs": available_handoffs(store),
                    "external": external,
                    "external_failed": failed,
                },
            )
        elif args.command == "apply":
            if args.asof is None:
                raise ValueError("--asof is required")
            report = apply(
                store, directory, market=args.market, economic=args.economic, asof=args.asof
            )
            print(json.dumps(report, sort_keys=True))
        else:
            if args.lease_handle is None:
                raise ValueError("common integration writes require the publication lease")
            verify_held(store, _load_handle(args.lease_handle))
            generations = canonical_generations()
            if args.command == "inputs":
                if args.identity is None or args.asof is None:
                    raise ValueError("--identity and --asof are required")
                report = publish_inputs(
                    store,
                    market=args.market,
                    economic=args.economic,
                    identity=args.identity,
                    asof=args.asof,
                    generations=generations,
                )
                print(json.dumps(report, sort_keys=True))
                return int(bool(report["failed"]))
            report = json.loads((directory / "applied.json").read_text(encoding="utf-8"))
            if report.get("external"):
                report["external_results"] = verify_and_seal(
                    store,
                    report["external"],
                    handle=_load_handle(args.lease_handle),
                    directory=directory / "external-readback",
                )
            for outcome in report["handoffs"]:
                if outcome["entry"].get("invalid"):
                    continue
                seal_integrated(
                    store,
                    outcome["entry"],
                    generations=generations,
                    recovery=outcome["recovery"],
                )
            report["canonical_generations"] = generations
            write_json_atomic(directory / "applied.json", report)
    except Exception as error:
        print(f"common source integration failed: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
