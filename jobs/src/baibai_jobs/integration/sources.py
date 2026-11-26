"""One scheduled canonical integration, independent of acquisition completion."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections.abc import Iterable
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.providers.articles.contracts import (
    SOURCES as _RETIRED_ARTICLE_SOURCES,
)
from baibai_engine.data_collection.providers.jpx.receipt_store import (
    validate_receipts as validate_jpx_receipts,
)
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.edinet_inputs import (
    read_edinet_input,
    retain_edinet_input,
)
from baibai_engine.machine_analysis.observations.financials.edinet_integration import (
    extract_saved_edinet,
)
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.json import read_private
from baibai_engine.storage.objects.r2 import (
    Boto3R2Store,
    LakeCASConflict,
    LakePublishError,
    ObjectStore,
    R2Error,
)
from baibai_engine.storage.paths import MARKET_DB_PATH
from baibai_engine.storage.sqlite.merge import MergeError
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError as MarketSchemaError
from baibai_engine.storage.sqlite.source_cache.schema import (
    validate_current_schema as validate_market_schema,
)
from baibai_jobs.acquisition.economic_staging import ARTICLE_HANDOFF_NAMESPACE, stage_prefix
from baibai_jobs.acquisition.receipts import (
    available_handoffs,
    download_fixed,
    seal_integrated,
)
from baibai_jobs.acquisition.source_inputs import GuardedInputStore
from baibai_jobs.integration.external_assessment_ready import available_ready
from baibai_jobs.integration.external_assessments import merge_ready, verify_and_seal
from baibai_jobs.integration.source_handoff import _JPX_COLLECTION, _replay
from baibai_jobs.publication.jpx_verification import (
    capture_publication as capture_jpx_publication,
)
from baibai_jobs.publication.jpx_verification import (
    verify_publication as verify_jpx_publication,
)
from baibai_jobs.publication.lease import _load_handle, verify_held
from baibai_jobs.publication.machine_bundle import read_published_machine
from baibai_jobs.publication.machine_origin import current_daily_run
from baibai_jobs.publication.snapshot_publication import daily_external_readback_dir


def _plan_progress(phase: str, started: float | None = None) -> float:
    now = time.perf_counter()
    state = "start" if started is None else f"done elapsed_seconds={now - started:.3f}"
    print(f"source plan: phase={phase} state={state}", file=sys.stderr, flush=True)
    return now


def fix_edinet_input(store: ObjectStore, directory: Path) -> InputSnapshot:
    """Freeze the existing EDINET owners for either a normal or exact recovery plan."""
    from baibai_engine.data_collection.providers.edinet.archive_native import EdinetArchiveL1
    from baibai_engine.data_collection.providers.edinet.list_native import EdinetListL1

    lists = EdinetListL1(store, directory / "edinet-native").freeze()
    archives = EdinetArchiveL1(store, directory / "edinet-archives-native").freeze()
    return retain_edinet_input(store, lists, archives, selected_at=datetime.now(UTC))


def _archived_equity_tradingview(entry: dict[str, Any]) -> bool:
    return (
        entry.get("market") is True
        and entry.get("namespace") in (None, "market-sources")
        and entry.get("source") == "tradingview"
    )


def _retired_article(entry: dict[str, object]) -> bool:
    return (
        entry.get("namespace") == ARTICLE_HANDOFF_NAMESPACE
        or entry.get("source") in _RETIRED_ARTICLE_SOURCES
    )


class RetiredArticleHandoffError(ValueError):
    """Old stages can only be inspected with the fixed evidence reader."""


def _require_current_handoffs(entries: Iterable[dict[str, object]]) -> None:
    for entry in entries:
        if _retired_article(entry):
            raise RetiredArticleHandoffError(
                "article handoffs retired; read exact evidence instead"
            )
        if _archived_equity_tradingview(entry):
            raise ValueError("individual TradingView handoffs are archived; create a new plan")


def _read_plan(directory: Path) -> dict[str, Any]:
    plan: dict[str, Any] = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
    if plan.keys() - {"handoffs", "edinet_input", "external", "external_failed"}:
        raise ValueError("unsupported integration plan fields; create a new plan")
    _require_current_handoffs(plan["handoffs"])
    return plan


def apply(
    store: Boto3R2Store,
    directory: Path,
    *,
    market: Path,
    asof: date,
) -> dict[str, object]:
    return _apply_plan(store, directory, market=market, asof=asof, plan=_read_plan(directory))


def _apply_plan(
    store: Boto3R2Store,
    directory: Path,
    *,
    market: Path,
    asof: date,
    plan: dict[str, Any],
) -> dict[str, object]:
    if any(
        entry.get("source") in {"jquants-market", "jquants-financial"} for entry in plan["handoffs"]
    ):
        raise ValueError("J-Quants does not publish market handoffs; create a new plan")
    if any(not entry["market"] for entry in plan["handoffs"]):
        raise ValueError("economic handoffs require the independent L1 integration")
    if plan.get("edinet_input") is None:
        raise ValueError("fixed EDINET input is absent; create a new integration plan")
    edinet_input = read_edinet_input(store, InputSnapshot.model_validate(plan["edinet_input"]))
    # Only this owner's canonical corruption is global and fatal.
    with closing(sqlite3.connect(market.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        validate_market_schema(conn)
    from baibai_jobs.integration.edinet_archive_native import project_archive_execution
    from baibai_jobs.integration.edinet_list_native import project_execution_lists

    native_archives = project_archive_execution(edinet_input.archives, market)
    native_lists, list_evidence = project_execution_lists(edinet_input.lists, market)
    outcomes: list[dict[str, Any]] = []
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
                        if entry["source"] in _JPX_COLLECTION:
                            validate_jpx_receipts(raw)
                        else:
                            validate_market_schema(raw)
                except sqlite3.DatabaseError as error:
                    raise ValueError("invalid source SQLite receipt") from error
                replay = _replay(entry["source"], market, packet)
                result = replay.report
                failed = recovery = not replay.complete
                applied = True
            except (R2Error, LakeCASConflict):
                raise
            except (
                LakePublishError,
                ValueError,
                MergeError,
                MarketSchemaError,
                JQuantsProviderError,
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
    extractions = extract_saved_edinet(
        market,
        asof=asof,
        metric_archives=edinet_input.archives,
        fact_archives=edinet_input.archives,
        fixed_list_evidence=list_evidence,
    )
    extractions["native_lists"] = native_lists
    extractions["native_archives"] = native_archives
    report: dict[str, object] = {
        "asof": asof.isoformat(),
        "handoffs": outcomes,
        "extractions": extractions,
        "edinet_input": edinet_input.identity.model_dump(mode="json"),
        "external": external,
        "external_failed": plan.get("external_failed", []),
    }
    jpx_bundles = [
        bundle_id
        for outcome in outcomes
        if outcome["entry"]["source"] in _JPX_COLLECTION and outcome["applied"]
        for bundle_id in (
            *outcome["result"].get("retained_bundles", []),
            *outcome["result"].get("replayed_bundles", []),
        )
    ]
    if jpx_bundles:
        report["jpx_publication"] = capture_jpx_publication(market, jpx_bundles)
    write_json_atomic(directory / "applied.json", report)
    return report


def canonical_generations(store: ObjectStore) -> dict[str, str]:
    pointer, _ = read_published_machine(store)
    return {
        "lake_release_id": pointer.release_id,
        "lake_manifest_sha256": pointer.manifest_sha256,
    }


def _apply_summary(report: dict[str, Any]) -> dict[str, object]:
    handoffs = report["handoffs"]
    extractions = report["extractions"]
    external = report["external"]
    return {
        "asof": report["asof"],
        "handoff_count": len(handoffs),
        "applied_handoff_count": sum(item["applied"] for item in handoffs),
        "failed_handoff_count": sum(item["failed"] for item in handoffs),
        "extraction_count": len(extractions),
        "failed_extraction_count": sum(bool(item["exit_code"]) for item in extractions.values()),
        "external_ready_failure_count": len(report["external_failed"]),
        "external": None
        if external is None
        else {
            "applied_plan_count": len(external["plans"]),
            "failed_plan_count": len(external["failed"]),
            "changed_partition_count": len(external["changed"]),
            "selected_receipt_count": len(external["selected_digests"]),
        },
    }


def _jpx_recovery_handoffs(
    store: ObjectStore, selections: list[list[str]]
) -> list[dict[str, object]]:
    """Select only named failed stages; retain their original identity and clock."""
    entries: list[dict[str, object]] = []
    seen: set[str] = set()
    for source, stage_id, digest in selections:
        if source not in _JPX_COLLECTION or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("JPX recovery requires a JPX source and exact manifest digest")
        if stage_id in seen:
            raise ValueError("duplicate JPX recovery stage")
        seen.add(stage_id)
        prefix = stage_prefix(stage_id, market=True)
        saved = read_private(store, prefix + "/handoff.json")
        if saved is None or hashlib.sha256(saved.payload).hexdigest() != digest:
            raise LakePublishError("exact JPX recovery manifest differs or is missing")
        report = json.loads(saved.payload)["result"]
        clock = datetime.fromisoformat(report["started_at"])
        if report["source"] != source or clock.utcoffset() is None:
            raise ValueError("JPX recovery source or original clock differs")
        date.fromisoformat(report["asof"])
        saved_marker = read_private(store, prefix + "/integrated.json")
        marker = {} if saved_marker is None else json.loads(saved_marker.payload)
        if (
            marker.get("status") != "needs_explicit_recovery"
            or marker.get("manifest_sha256") != digest
            or marker.get("source") != source
            or datetime.fromisoformat(marker["started_at"]) != clock
        ):
            raise ValueError("JPX recovery requires the matching failed integration marker")
        if read_private(store, prefix + f"/recovered-{digest}.json") is not None:
            raise ValueError("JPX stage already has an explicit recovery receipt")
        entries.append(
            {
                "market": True,
                "source": source,
                "stage_id": stage_id,
                "manifest_sha256": digest,
                "started_at": report["started_at"],
                "asof": report["asof"],
                "integration": marker,
                "explicit_jpx_recovery": True,
            }
        )
    return entries


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "apply", "seal", "check"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--market", type=Path, default=MARKET_DB_PATH)
    parser.add_argument("--asof", type=date.fromisoformat)
    parser.add_argument("--summary", action="store_true", help="Log only apply counts")
    parser.add_argument("--lease-handle", type=Path)
    parser.add_argument(
        "--daily-readback-notice", type=Path, help="reuse this attempt's verified Lake bytes"
    )
    parser.add_argument(
        "--recover-jpx-stage",
        nargs=3,
        action="append",
        metavar=("SOURCE", "STAGE_ID", "MANIFEST_SHA256"),
        help="Plan only these exact failed JPX stages; do not discover other pending sources",
    )
    return parser


def _daily_external_readback(
    notice_path: Path | None, directory: Path, *, bucket: str, generations: dict[str, str]
) -> Path | None:
    if notice_path is None:
        return None
    try:
        run = current_daily_run()
        notice = json.loads(notice_path.read_bytes())
        if (
            notice["daily_external_readback"] is not True
            or notice["models_ready"] is not True
            or notice["daily_run"] != run
            or notice["lake"]["release_id"] != generations["lake_release_id"]
            or notice["lake"]["release_manifest_sha256"] != generations["lake_manifest_sha256"]
        ):
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return (
        daily_external_readback_dir(directory, bucket=bucket, run=run)
        / generations["lake_manifest_sha256"]
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.summary and args.command != "apply":
        print("--summary is only valid with apply", file=sys.stderr)
        return 1
    seal_phase = "setup"
    try:
        if args.recover_jpx_stage and args.command != "plan":
            raise ValueError("--recover-jpx-stage is only valid with plan")
        if args.daily_readback_notice is not None and args.command != "seal":
            raise ValueError("--daily-readback-notice is only valid with seal")
        directory = args.directory
        plan: dict[str, Any] | None = None
        if args.command == "apply":
            if args.asof is None:
                raise ValueError("--asof is required")
            plan = _read_plan(directory)
        sealed_report: dict[str, Any] | None = None
        if args.command == "seal":
            if args.lease_handle is None:
                raise ValueError("common integration writes require the publication lease")
            sealed_report = json.loads((directory / "applied.json").read_text(encoding="utf-8"))
            _require_current_handoffs(item["entry"] for item in sealed_report["handoffs"])
        if args.command == "check":
            report = json.loads((directory / "applied.json").read_text())
            integration_failed = (
                any(item["failed"] for item in report["handoffs"])
                or any(item["exit_code"] for item in report["extractions"].values())
                or bool(report.get("external_failed"))
                or bool((report.get("external") or {}).get("failed"))
            )
            source_gaps = [
                {
                    "source": item["entry"]["source"],
                    "failures": item["result"].get("acquisition_failures", []),
                    "issues": item["result"].get("acquisition_issues", []),
                }
                for item in report["handoffs"]
                if item.get("result", {}).get("acquisition_failures")
                or item.get("result", {}).get("acquisition_issues")
            ]
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
                        else "completed_with_source_gaps"
                        if source_gaps
                        else "completed_with_deferred_inputs"
                        if deferred
                        else "completed",
                        "deferred_inputs": deferred,
                        "source_gaps": source_gaps,
                        "failed_handoffs": [
                            {
                                "source": item["entry"]["source"],
                                "stage_id": item["entry"]["stage_id"],
                                **(
                                    {"asof": item["entry"]["asof"]}
                                    if "asof" in item["entry"]
                                    else {}
                                ),
                                "result": item["result"],
                            }
                            for item in report["handoffs"]
                            if item["failed"]
                        ],
                        "failed_extractions": {
                            source: item
                            for source, item in report["extractions"].items()
                            if item["exit_code"]
                        },
                    },
                    sort_keys=True,
                )
            )
            return int(integration_failed)
        bucket = os.environ.get("R2_STORES_BUCKET") or "baibai-stores"
        with closing(Boto3R2Store(bucket=bucket)) as store:
            directory.mkdir(parents=True, exist_ok=True)
            if args.command == "plan":
                if (directory / "plan.json").exists():
                    raise FileExistsError("integration inputs are already fixed")
                if args.recover_jpx_stage:
                    handoffs = _jpx_recovery_handoffs(store, args.recover_jpx_stage)
                    external: list[dict[str, str]] = []
                    failed: list[dict[str, str]] = []
                else:
                    started = _plan_progress("external_ready")
                    external, failed = available_ready(store)
                    _plan_progress("external_ready", started)
                    started = _plan_progress("market_handoffs")
                    market_handoffs = available_handoffs(
                        store, market=True, exclude_equity_tradingview=True
                    )
                    _plan_progress("market_handoffs", started)
                    handoffs = [
                        entry
                        for entry in market_handoffs
                        if not _retired_article(entry)
                        and entry.get("source")
                        not in {
                            "jquants-market",
                            "jquants-financial",
                            "edinet-list",
                            "edinet-metrics",
                            "edinet-facts",
                        }
                    ]
                started = _plan_progress("edinet_input")
                edinet_input = fix_edinet_input(store, directory).model_dump(mode="json")
                _plan_progress("edinet_input", started)
                write_json_atomic(
                    directory / "plan.json",
                    {
                        "handoffs": handoffs,
                        "edinet_input": edinet_input,
                        "external": external,
                        "external_failed": failed,
                    },
                )
            elif args.command == "apply":
                assert plan is not None
                assert args.asof is not None
                report = _apply_plan(
                    store, directory, market=args.market, asof=args.asof, plan=plan
                )
                if args.summary:
                    print(json.dumps(_apply_summary(report), sort_keys=True), flush=True)
                    return 0
                public_report = dict(report)
                if "jpx_publication" in public_report:
                    public_report["jpx_publication"] = "private readback plan"
                print(json.dumps(public_report, sort_keys=True))
            else:
                assert args.command == "seal"
                assert args.lease_handle is not None
                assert sealed_report is not None
                report = sealed_report
                seal_phase = "publication_lease"
                handle = _load_handle(args.lease_handle)
                verify_held(store, handle)
                seal_phase = "canonical_machine"
                generations = canonical_generations(store)
                guarded = GuardedInputStore(store, lambda: verify_held(store, handle))
                if any(
                    item["entry"]["source"] in _JPX_COLLECTION
                    and item["applied"]
                    and item["result"]["retained_bundles"]
                    for item in report["handoffs"]
                ):
                    seal_phase = "jpx_readback"
                    report["jpx_readback"] = verify_jpx_publication(
                        store,
                        expected=report.get("jpx_publication", {}),
                        directory=directory / "jpx-readback",
                        generations=generations,
                    )
                if report.get("external"):
                    seal_phase = "external_readback_and_seal"
                    readback_directory = _daily_external_readback(
                        args.daily_readback_notice,
                        directory,
                        bucket=bucket,
                        generations=generations,
                    )
                    report["external_results"] = verify_and_seal(
                        store,
                        report["external"],
                        handle=handle,
                        directory=directory / "external-readback",
                        same_run_readback=readback_directory,
                    )
                for outcome in report["handoffs"]:
                    if not outcome["entry"]["market"]:
                        raise ValueError(
                            "economic handoffs must be sealed by economic L1 integration"
                        )
                    if outcome["entry"].get("invalid"):
                        continue
                    seal_phase = "handoff_terminal"
                    seal_integrated(
                        guarded,
                        outcome["entry"],
                        generations=generations,
                        recovery=outcome["recovery"],
                    )
                seal_phase = "local_report"
                report["canonical_generations"] = generations
                write_json_atomic(directory / "applied.json", report)
                seal_phase = "close"
    except Exception as error:
        if args.command == "seal" and isinstance(error, R2Error):
            cause = type(error.__cause__).__name__ if error.__cause__ is not None else "none"
            status = error.http_status
            print(
                f"source seal failed: phase={seal_phase} exception={type(error).__name__} "
                f"cause={cause} status={status if type(status) is int else 'unknown'}",
                file=sys.stderr,
            )
        print(f"common source integration failed: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
