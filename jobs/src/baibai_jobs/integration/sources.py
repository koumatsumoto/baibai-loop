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
from contextlib import closing, suppress
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

from baibai_engine.data_collection.providers.jpx.receipt_store import (
    validate_receipts as validate_jpx_receipts,
)
from baibai_engine.data_collection.receipts.articles.store import validate_receipt_schema
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.edinet_inputs import (
    read_edinet_input,
    retain_edinet_input,
)
from baibai_engine.machine_analysis.observations.external_assessments.tradingview import (
    observations as observations_owner,
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
from baibai_jobs.acquisition.article_planning import (
    ARTICLE_NAMESPACE,
    ARTICLE_SOURCES,
    COLLECTION_SOURCES,
    pending_handoffs,
    publish_planning_input,
)
from baibai_jobs.acquisition.economic_staging import stage_prefix
from baibai_jobs.acquisition.inputs import SOURCE_INPUT_GROUPS
from baibai_jobs.acquisition.receipts import available_handoffs, download_fixed, seal_integrated
from baibai_jobs.acquisition.source_inputs import GuardedInputStore, publish_inputs
from baibai_jobs.integration.external_assessment_ready import available_ready
from baibai_jobs.integration.external_assessments import merge_ready, verify_and_seal
from baibai_jobs.integration.source_handoff import _JPX_COLLECTION, _replay
from baibai_jobs.publication.article_verification import capture_delta, verify_delta
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
from baibai_jobs.publication.tradingview_verification import (
    capture_publication as capture_tradingview_publication,
)
from baibai_jobs.publication.tradingview_verification import (
    verify_publication as verify_tradingview_publication,
)


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


def apply(
    store: Boto3R2Store,
    directory: Path,
    *,
    market: Path,
    asof: date,
) -> dict[str, object]:
    plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
    if plan.keys() - {"handoffs", "edinet_input", "external", "external_failed"}:
        raise ValueError("unsupported integration plan fields; create a new plan")
    if any(
        entry.get("source") in {"jquants-market", "jquants-financial"} for entry in plan["handoffs"]
    ):
        raise ValueError("J-Quants does not publish market handoffs; create a new plan")
    if any(not entry["market"] for entry in plan["handoffs"]):
        raise ValueError("economic handoffs require the independent L1 integration")
    if plan.get("edinet_input") is None:
        raise ValueError("fixed EDINET input is absent; create a new integration plan")
    edinet_input = read_edinet_input(store, InputSnapshot.model_validate(plan["edinet_input"]))
    # Article and JPX replay consume neither a J-Quants head nor a full-history
    # selection. The later model/publication step fixes its own source inputs.
    master_input = calendar_input = None
    if any(
        entry["source"] == "tradingview"
        and not (entry.get("invalid") or entry.get("late_before_integrated"))
        for entry in plan["handoffs"]
    ):
        from baibai_engine.data_collection.providers.jquants.calendar_native import CalendarL1
        from baibai_engine.data_collection.providers.jquants.master_native import MasterL1
        from baibai_engine.machine_analysis.generation.full_day_inputs import EmptyFullDayInput
        from baibai_jobs.integration.calendar_native import fixed_input as fixed_calendar_input
        from baibai_jobs.integration.master_native import fixed_input as fixed_master_input

        evaluated_at = datetime.now(UTC)
        calendar = CalendarL1(store, directory / "calendar-native").fixed()
        master = MasterL1(store, directory / "master-native").fixed()
        # An initialized empty head blocks only TV. Validate both authorities
        # independently so an empty head cannot hide corruption in the other one.
        with suppress(EmptyFullDayInput):
            calendar_input = fixed_calendar_input(
                store, calendar, as_of=asof, evaluated_at=evaluated_at
            )
        with suppress(EmptyFullDayInput):
            master_input = fixed_master_input(store, master, as_of=asof, evaluated_at=evaluated_at)
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
        elif entry["source"] == "tradingview" and (master_input is None or calendar_input is None):
            result = {"reason": "tradingview_native_context_unavailable"}
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
                        if entry["source"] in ARTICLE_SOURCES:
                            validate_receipt_schema(raw)
                        elif entry["source"] in _JPX_COLLECTION:
                            validate_jpx_receipts(raw)
                        else:
                            validate_market_schema(raw)
                except sqlite3.DatabaseError as error:
                    raise ValueError("invalid source SQLite receipt") from error
                replay = _replay(
                    entry["source"],
                    market,
                    packet,
                    store=store,
                    master_input=master_input,
                    calendar_input=calendar_input,
                )
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
                observations_owner.FetchError,
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
    for outcome in outcomes:
        if outcome["entry"].get("namespace") == ARTICLE_NAMESPACE and outcome["applied"]:
            outcome["article_delta"] = capture_delta(
                market,
                outcome["result"]["receipt_ids"],
                artifact_ids=outcome["result"].get("artifact_ids", ()),
            )
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
    tradingview = [
        outcome["result"]
        for outcome in outcomes
        if outcome["entry"]["source"] == "tradingview" and outcome["applied"]
    ]
    if tradingview:
        report["tradingview_publication"] = capture_tradingview_publication(
            market,
            [date.fromisoformat(result["snapshot_date"]) for result in tradingview],
            receipt_ids=[identity for result in tradingview for identity in result["receipt_ids"]],
        )
    write_json_atomic(directory / "applied.json", report)
    return report


def canonical_generations(store: ObjectStore) -> dict[str, str]:
    pointer, _ = read_published_machine(store)
    return {
        "lake_release_id": pointer.release_id,
        "lake_manifest_sha256": pointer.manifest_sha256,
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


def _tv_quality_day(report: dict[str, Any]) -> str | None:
    """Recognize the fixed-receipt failure, not a generic failed source check."""
    match report:
        case {
            "asof": str(day),
            "handoffs": list(handoffs),
            "extractions": dict(extractions),
            "external_failed": [],
            "external": None | {"failed": []},
        }:
            pass
        case _:
            return None
    try:
        if date.fromisoformat(day).isoformat() != day:
            return None
    except ValueError:
        return None
    if set(extractions) != {
        "edinet-facts",
        "edinet-metrics",
        "native_lists",
        "native_archives",
    } or any(
        not isinstance(item, dict)
        or type(item.get("exit_code")) is not int
        or item["exit_code"] != 0
        or item.get("status") != "completed"
        or item.get("deferred_inputs")
        for item in extractions.values()
    ):
        return None
    rejected_stage = False
    for outcome in handoffs:
        match outcome:
            case {
                "entry": {
                    "market": True,
                    "late_before_integrated": False,
                    "integration": None,
                } as entry,
                "applied": True,
                "failed": bool(failed),
                "recovery": bool(recovery),
            } if "invalid" not in entry and failed == recovery:
                pass
            case _:
                return None
        if not failed:
            continue
        if rejected_stage or entry.get("source") != "tradingview" or entry.get("asof") != day:
            return None
        result = outcome.get("result")
        if (
            not isinstance(result, dict)
            or "reason" in result
            or result.get("snapshot_date") != day
            or any(
                type(result.get(key)) is not int or result[key] != 0
                for key in ("rows", "rows_before", "rows_added", "unresolved")
            )
            or type(result.get("expected_universe")) is not int
            or result["expected_universe"] <= 0
            or result.get("remaining") != result["expected_universe"]
        ):
            return None
        fixed, rejected = result.get("receipt_ids"), result.get("rejected_receipts")
        if not isinstance(fixed, list) or not fixed or not isinstance(rejected, list):
            return None
        if not all(isinstance(ident, str) and ident for ident in fixed):
            return None
        # All of the fixed originals must be accounted for once, not just a nonempty reason list.
        ids = []
        for item in rejected:
            match item:
                case {"receipt_id": str(identity), "reason": "provider_response"}:
                    ids.append(identity)
                case _:
                    return None
        if len(set(fixed)) != len(fixed) or sorted(ids) != sorted(fixed):
            return None
        rejected_stage = True
    return day if rejected_stage else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "apply", "seal", "inputs", "check"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--market", type=Path, default=MARKET_DB_PATH)
    parser.add_argument("--asof", type=date.fromisoformat)
    parser.add_argument("--identity")
    parser.add_argument("--group", help="Reissue only this scoped input group")
    parser.add_argument("--lease-handle", type=Path)
    parser.add_argument(
        "--daily-readback-notice", type=Path, help="reuse this attempt's verified Lake bytes"
    )
    parser.add_argument(
        "--quality-output", type=Path, help="Actions step output for the daily check"
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
    if args.group is not None and args.command != "inputs":
        print("--group is only valid with inputs", file=sys.stderr)
        return 1
    try:
        if (
            args.group is not None
            and args.group not in SOURCE_INPUT_GROUPS
            and args.group not in COLLECTION_SOURCES
        ):
            raise ValueError("unknown market acquisition input group")
        if args.recover_jpx_stage and args.command != "plan":
            raise ValueError("--recover-jpx-stage is only valid with plan")
        if args.quality_output and args.command != "check":
            raise ValueError("--quality-output is only valid with check")
        if args.daily_readback_notice is not None and args.command != "seal":
            raise ValueError("--daily-readback-notice is only valid with seal")
        directory = args.directory
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
            if integration_failed and args.quality_output and (day := _tv_quality_day(report)):
                with args.quality_output.open("a", encoding="utf-8") as output:
                    print(f"tv_quality_asof={day}", file=output)
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
                    market_handoffs = available_handoffs(store, market=True)
                    _plan_progress("market_handoffs", started)
                    started = _plan_progress("article_handoffs")
                    article_handoffs = pending_handoffs(store)
                    _plan_progress("article_handoffs", started)
                    handoffs = [
                        entry
                        for entry in [*market_handoffs, *article_handoffs]
                        if entry.get("source")
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
                if args.asof is None:
                    raise ValueError("--asof is required")
                report = apply(store, directory, market=args.market, asof=args.asof)
                public_report = dict(report)
                if "jpx_publication" in public_report:
                    public_report["jpx_publication"] = "private readback plan"
                if "tradingview_publication" in public_report:
                    public_report["tradingview_publication"] = "private readback plan"
                public_report["handoffs"] = [
                    {
                        **item,
                        "article_delta": "private readback plan",
                        "result": {
                            "receipt_count": len(item["result"].get("receipt_ids", [])),
                            "observation_count": len(item["result"].get("observation_ids", [])),
                            "artifact_count": len(item["result"].get("artifact_ids", [])),
                            "failed": item["failed"],
                        },
                    }
                    if item["entry"].get("namespace") == ARTICLE_NAMESPACE
                    else item
                    for item in cast(list[dict[str, Any]], report["handoffs"])
                ]
                print(json.dumps(public_report, sort_keys=True))
            else:
                if args.lease_handle is None:
                    raise ValueError("common integration writes require the publication lease")
                handle = _load_handle(args.lease_handle)
                verify_held(store, handle)
                generations = canonical_generations(store)
                guarded = GuardedInputStore(store, lambda: verify_held(store, handle))
                if args.command == "inputs":
                    if args.identity is None or args.asof is None:
                        raise ValueError("--identity and --asof are required")
                    input_report: dict[str, Any] = {"updated": [], "failed": []}
                    groups = (
                        SOURCE_INPUT_GROUPS
                        if args.group is None
                        else (args.group,)
                        if args.group in SOURCE_INPUT_GROUPS
                        else ()
                    )
                    for group in groups:
                        result = publish_inputs(
                            guarded,
                            market=args.market,
                            economic=directory / "unused-market-input",
                            identity=args.identity,
                            asof=args.asof,
                            generations=generations,
                            only_group=group,
                        )
                        input_report["updated"].extend(result["updated"])
                        input_report["failed"].extend(result["failed"])
                    for source in (
                        COLLECTION_SOURCES
                        if args.group is None
                        else (args.group,)
                        if args.group in COLLECTION_SOURCES
                        else ()
                    ):
                        try:
                            publish_planning_input(guarded, source, generations)
                        except (ValueError, LakeCASConflict) as error:
                            input_report["failed"].append(
                                {"group": source, "reason": type(error).__name__}
                            )
                        else:
                            input_report["updated"].append(source)
                    print(json.dumps(input_report, sort_keys=True))
                    return int(bool(input_report["failed"]))
                report = json.loads((directory / "applied.json").read_text(encoding="utf-8"))
                if any(
                    item["entry"]["source"] in _JPX_COLLECTION
                    and item["applied"]
                    and item["result"]["retained_bundles"]
                    for item in report["handoffs"]
                ):
                    report["jpx_readback"] = verify_jpx_publication(
                        store,
                        expected=report.get("jpx_publication", {}),
                        directory=directory / "jpx-readback",
                        generations=generations,
                    )
                if report.get("external"):
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
                if any(
                    item["entry"]["source"] == "tradingview" and item["applied"]
                    for item in report["handoffs"]
                ):
                    report["tradingview_readback"] = verify_tradingview_publication(
                        store,
                        expected=report.get("tradingview_publication", {}),
                        directory=directory / "tradingview-readback",
                        generations=generations,
                    )
                for outcome in report["handoffs"]:
                    if not outcome["entry"]["market"]:
                        raise ValueError(
                            "economic handoffs must be sealed by economic L1 integration"
                        )
                    if outcome["entry"].get("invalid"):
                        continue
                    if (
                        outcome["entry"].get("namespace") == ARTICLE_NAMESPACE
                        or outcome["entry"]["source"] == "tradingview"
                    ) and not outcome["applied"]:
                        # Keep an uncaptured handoff discoverable for repair, not sealed.
                        continue
                    if (
                        outcome["entry"].get("namespace") == ARTICLE_NAMESPACE
                        and outcome["applied"]
                    ):
                        outcome["article_readback"] = verify_delta(
                            store,
                            delta=outcome["article_delta"],
                            directory=directory / "article-readback",
                            generations=generations,
                        )
                    seal_integrated(
                        guarded,
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
