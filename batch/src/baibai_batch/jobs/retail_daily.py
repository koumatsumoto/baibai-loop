"""Manual fixed-cohort collection and private handoff; scheduling remains disabled."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from baibai_batch.jobs.retail_cohorts import (
    RunPlan,
    make_plan,
    mark_inflight,
    new_journal,
    record_outcome,
    summarize,
    validate_resume,
)
from baibai_batch.storage.lake_publish import Boto3R2Store, ObjectStore
from baibai_batch.storage.publish_market_lake import _resolve_serving_release, _serving_pointer
from baibai_batch.storage.retail_staging import (
    Checkpoint,
    checkpoint_bytes,
    checkpoint_key,
    put_private,
    read_checkpoints,
    read_plan,
    read_terminal,
    save_new_plan,
)
from baibai_engine.batch_api import (
    MARKET_DB_PATH,
    LakeObjectCache,
    RetailCollectionResult,
    RetailSource,
    lake_session,
    retail_collect_pair,
    retail_collector_binding,
    retail_frozen_lake_universe,
)

SOFT_BUDGET_SECONDS = 4 * 60 * 60
PAIR_AND_CHECKPOINT_RESERVE_SECONDS = 15 * 60


def require_binding(plan: RunPlan) -> None:
    if plan.collector_binding != retail_collector_binding():
        raise ValueError("retail collector binding changed; do not reuse this batch")


class _MasterSource:
    def __init__(self, store: ObjectStore) -> None:
        self.store = store

    def read_bytes(self, key: str) -> bytes:
        return self.store.get_bytes(key)

    def uri(self, key: str) -> str:
        raise ValueError("retail master source requires materialized local objects")


def initialize(
    store: ObjectStore, *, run_id: str, master_asof: date, mirror: Path | None
) -> RunPlan:
    existing = read_plan(store, run_id)
    if existing is not None:
        require_binding(existing)
        if json.loads(existing.universe_binding)["master_asof"] != master_asof.isoformat():
            raise ValueError("retail saved master assertion changed")
        return existing
    if mirror is None:
        raise ValueError("private master cache directory required for a new plan")
    mirror.mkdir(mode=0o700, parents=True, exist_ok=True)
    mirror.chmod(0o700)
    pointer = _serving_pointer(store).pointer
    if pointer is None:
        raise ValueError("retail plan requires an existing L1 release")
    release = _resolve_serving_release(store, mirror, pointer)
    with lake_session() as session:
        tickers, binding = retail_frozen_lake_universe(
            session,
            release=release,
            cache=LakeObjectCache(root=mirror, source=_MasterSource(store)),
            master_asof=master_asof,
        )
    plan = make_plan(
        run_id=run_id,
        tickers=tickers,
        universe_binding=binding,
        collector_binding=retail_collector_binding(),
    )
    if len(plan.cohorts) > 256:
        raise ValueError("retail cohort count exceeds the supported Actions matrix")
    save_new_plan(store, plan)
    return plan


def _acquire_pair(
    store: ObjectStore,
    plan: RunPlan,
    checkpoint: Checkpoint,
    etag: str | None,
    source: RetailSource,
    ticker: str,
) -> tuple[Checkpoint, str | None, RetailCollectionResult]:
    marked = False
    pair_content_bytes = 0
    pair_finished = checkpoint.last_response_finished_at_utc

    def persist() -> None:
        nonlocal etag
        receipt = put_private(
            store,
            checkpoint_key(plan, checkpoint.journal.cohort_index),
            checkpoint_bytes(plan, checkpoint),
            previous_etag=etag,
        )
        etag = receipt.etag

    def before_request() -> None:
        nonlocal checkpoint, marked
        if not marked:
            checkpoint = replace(
                checkpoint,
                journal=mark_inflight(plan, checkpoint.journal, source, ticker),
            )
            marked = True
        checkpoint = replace(checkpoint, requests_started=checkpoint.requests_started + 1)
        persist()

    def on_response(finished: datetime, _requests: int, body_bytes: int) -> None:
        nonlocal pair_content_bytes, pair_finished
        pair_content_bytes += body_bytes
        pair_finished = finished

    def on_outcome(result: RetailCollectionResult) -> None:
        nonlocal checkpoint
        if len(result.outcomes) != 1 or len(result.observations) > 1:
            raise ValueError("retail pair result shape changed")
        checkpoint = replace(
            checkpoint,
            journal=record_outcome(
                plan,
                checkpoint.journal,
                result.outcomes[0],
                result.observations[0] if result.observations else None,
            ),
            confirmed_http_requests=checkpoint.confirmed_http_requests + result.requests,
            response_content_bytes=checkpoint.response_content_bytes + pair_content_bytes,
            last_response_finished_at_utc=pair_finished,
        )
        persist()

    result = retail_collect_pair(
        ticker,
        source=source,
        before_request=before_request,
        on_response=on_response,
        on_outcome=on_outcome,
    )
    return checkpoint, etag, result


def acquire_cohort(
    store: ObjectStore,
    plan: RunPlan,
    index: int,
    *,
    soft_budget_seconds: float = SOFT_BUDGET_SECONDS,
) -> dict[str, object]:
    """Workflow's fixed acquisition concurrency serializes all batches and resumes."""
    started = time.monotonic()
    if not 0 < soft_budget_seconds <= SOFT_BUDGET_SECONDS:
        raise ValueError("invalid retail soft budget")
    require_binding(plan)
    key = checkpoint_key(plan, index)
    terminal = read_terminal(store, plan)
    if terminal is not None:
        return {
            **terminal,
            "status": "already_published",
            "readback": "previously_verified",
            "confirmed_http_requests": 0,
            "request_reservations": 0,
        }
    saved = read_checkpoints(store, plan)
    validate_resume(plan, [checkpoint.journal for checkpoint, _ in saved.values()])
    checkpoint, etag = saved.get(index, (Checkpoint(new_journal(plan, index)), None))
    stop_reason = None

    for entry in tuple(checkpoint.journal.entries):
        if entry.state != "unattempted":
            continue
        # Leave a normal completed checkpoint, rather than rely on runner killing
        # a request. The pair's own real timer also includes its durable callbacks.
        if time.monotonic() - started + PAIR_AND_CHECKPOINT_RESERVE_SECONDS >= soft_budget_seconds:
            stop_reason = "soft_budget"
            break
        checkpoint, etag, result = _acquire_pair(
            store,
            plan,
            checkpoint,
            etag,
            entry.source,
            entry.ticker,
        )
        if result.stop_reason:
            stop_reason = result.stop_reason
            break
    # Even an untouched soft-stop cohort remains explicit, rather than disappearing.
    if etag is None:
        put_private(store, key, checkpoint_bytes(plan, checkpoint), previous_etag=None)
    report = summarize(plan, [checkpoint.journal])
    report.update(
        cohort_index=index,
        stop_reason=stop_reason,
        request_reservations=checkpoint.requests_started,
        confirmed_http_requests=checkpoint.confirmed_http_requests,
        response_content_bytes=checkpoint.response_content_bytes,
        response_bytes_basis="uncompressed fully received bodies; wire bytes unmeasured",
    )
    return report


def write_outputs(plan: RunPlan | None) -> None:
    values = {
        "exists": str(plan is not None).lower(),
        "cohorts": json.dumps([cohort.index for cohort in plan.cohorts]) if plan else "[]",
    }
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8") as stream:
            for key, value in values.items():
                print(f"{key}={value}", file=stream)


def write_publication_outputs(report: dict[str, object]) -> None:
    """Expose only measured aggregates; normal_null means all metric values null."""
    states = report.get("state_counts")
    states = states if isinstance(states, dict) else {}
    counters = {
        "expected_pairs": report.get("expected_pairs"),
        "typed_pairs": report.get("observations"),
        "known_failure_pairs": states.get("failure"),
        "normal_null_pairs": states.get("normal_null"),
        "rows_added": report.get("rows_added"),
    }
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8") as stream:
            for key, value in counters.items():
                safe = str(value) if type(value) is int and value >= 0 else "unknown"
                print(f"retail_{key}={safe}", file=stream)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "plan", "acquire", "audit", "publish"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--master-asof", type=date.fromisoformat)
    parser.add_argument("--cohort", type=int)
    parser.add_argument("--db", type=Path, default=MARKET_DB_PATH)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--lease-handle", type=Path)
    args = parser.parse_args(argv)
    try:
        plan: RunPlan | None
        report: dict[str, object]
        store = Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
        if args.command == "plan":
            if args.master_asof is None:
                raise ValueError("saved master date required")
            plan = initialize(
                store, run_id=args.run_id, master_asof=args.master_asof, mirror=args.directory
            )
        else:
            plan = read_plan(store, args.run_id)
        if args.command == "status":
            write_outputs(plan)
            print(json.dumps({"plan_exists": plan is not None}))
            return 0
        if plan is None:
            raise ValueError("retail fixed plan is missing")
        require_binding(plan)
        if args.command == "plan":
            write_outputs(plan)
            report = {
                "tickers": len(plan.tickers),
                "cohorts": len(plan.cohorts),
                "expected_pairs": len(plan.tickers) * len(plan.sources),
            }
        elif args.command == "acquire":
            if args.cohort is None:
                raise ValueError("cohort required")
            report = acquire_cohort(store, plan, args.cohort)
        elif args.command == "audit":
            checkpoints = read_checkpoints(store, plan)
            report = summarize(plan, [checkpoint.journal for checkpoint, _ in checkpoints.values()])
        else:
            if args.directory is None or args.lease_handle is None:
                raise ValueError("private publish directory and lease handle required")
            from baibai_batch.jobs.retail_publication import publish_and_verify
            from baibai_batch.storage.publication_lease import _load_handle

            report = publish_and_verify(
                store,
                plan,
                handle=_load_handle(args.lease_handle),
                sqlite_path=args.db,
                mirror=args.db.parents[1],
                directory=args.directory,
            )
            write_publication_outputs(report)
        print(json.dumps(report, sort_keys=True))
        if args.command == "acquire":
            if report.get("status") == "already_published":
                return 0
            journal = read_checkpoints(store, plan)[args.cohort][0].journal
            if journal.halt_reason is not None or any(
                entry.state == "unattempted" for entry in journal.entries
            ):
                return 1
        return 0
    except Exception as error:
        print(f"Retail daily failed; type={type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
