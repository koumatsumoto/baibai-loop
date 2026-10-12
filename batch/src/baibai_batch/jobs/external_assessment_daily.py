"""Daily and manual fixed-cohort collection through the same private handoff."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import requests

from baibai_batch.jobs.external_assessment_cohorts import (
    RunPlan,
    make_plan,
    new_result,
    record_outcome,
    summarize,
)
from baibai_batch.jobs.schedule import external_assessment_target_date
from baibai_batch.storage.external_assessment_identity import SCHEDULED_BATCH_PREFIX
from baibai_batch.storage.external_assessment_raw import RawBundle
from baibai_batch.storage.external_assessment_staging import (
    CohortStage,
    commit_stage,
    read_plan,
    read_stage,
    read_stages,
    read_terminal,
    save_new_plan,
    terminal_status,
)
from baibai_batch.storage.lake_publish import Boto3R2Store, ObjectStore
from baibai_batch.storage.publish_market_lake import _resolve_serving_release, _serving_pointer
from baibai_engine.batch_api import (
    MARKET_DB_PATH,
    ExternalAssessmentCollectionResult,
    ExternalAssessmentHostRateLimiter,
    ExternalAssessmentReceivedHTML,
    LakeObjectCache,
    external_assessment_calendar_master_asof,
    external_assessment_collect_pair,
    external_assessment_collector_binding,
    external_assessment_frozen_lake_universe,
    lake_session,
)

SOFT_BUDGET_SECONDS = 4 * 60 * 60
PAIR_AND_STAGE_RESERVE_SECONDS = 15 * 60
MAX_PAIR_RETRIES = 5
RETRY_BACKOFF_SECONDS = (5, 10, 20, 40, 80)


def require_binding(plan: RunPlan) -> None:
    if plan.collector_binding != external_assessment_collector_binding():
        raise ValueError("external assessment collector binding changed; do not reuse this batch")


class _MasterSource:
    def __init__(self, store: ObjectStore) -> None:
        self.store = store

    def read_bytes(self, key: str) -> bytes:
        return self.store.get_bytes(key)

    def uri(self, key: str) -> str:
        raise ValueError("external assessment master source requires materialized local objects")


def initialize(
    store: ObjectStore,
    *,
    run_id: str,
    master_asof: date | None,
    mirror: Path | None,
    require_existing: bool = False,
    observation_date: date | None = None,
) -> RunPlan:
    if (master_asof is None) == (observation_date is None):
        raise ValueError(
            "external assessment plan requires either a saved master or a scheduled date"
        )
    existing = read_plan(store, run_id)
    if existing is not None:
        saved_master = date.fromisoformat(json.loads(existing.universe_binding)["master_asof"])
        if master_asof is not None and saved_master != master_asof:
            raise ValueError("external assessment saved master assertion changed")
        if observation_date is not None and saved_master > observation_date:
            raise ValueError("external assessment saved master is after the scheduled date")
        return existing
    if require_existing:
        raise ValueError("external assessment partial publication requires an existing fixed plan")
    if mirror is None:
        raise ValueError("private master cache directory required for a new plan")
    mirror.mkdir(mode=0o700, parents=True, exist_ok=True)
    mirror.chmod(0o700)
    pointer = _serving_pointer(store).pointer
    if pointer is None:
        raise ValueError("external assessment plan requires an existing L1 release")
    release = _resolve_serving_release(store, mirror, pointer)
    with lake_session() as session:
        cache = LakeObjectCache(root=mirror, source=_MasterSource(store))
        if observation_date is not None:
            master_asof = external_assessment_calendar_master_asof(
                session, release=release, cache=cache, observation_date=observation_date
            )
        assert master_asof is not None
        tickers, binding = external_assessment_frozen_lake_universe(
            session,
            release=release,
            cache=cache,
            master_asof=master_asof,
        )
    plan = make_plan(
        run_id=run_id,
        tickers=tickers,
        universe_binding=binding,
        collector_binding=external_assessment_collector_binding(),
    )
    if len(plan.cohorts) > 256:
        raise ValueError("external assessment cohort count exceeds the supported Actions matrix")
    save_new_plan(store, plan)
    return plan


def _terminal_result(terminal: dict[str, object]) -> dict[str, object]:
    return {
        **terminal,
        "status": terminal_status(terminal),
        "readback": "previously_verified",
        "rows_added": 0,
        "uploaded_bytes": 0,
        "uploaded_objects": 0,
        "publication_transfers": None,
    }


def _wait_for_retry(deadline: datetime, *, started: float, budget: float) -> bool:
    while True:
        waiting = max(0.0, (deadline - datetime.now(UTC)).total_seconds())
        if time.monotonic() - started + waiting + PAIR_AND_STAGE_RESERVE_SECONDS >= budget:
            return False
        if waiting == 0:
            return True
        time.sleep(min(waiting, 30.0))


def _raw_warning() -> None:
    # Provider exceptions may contain credentials, signed URLs or response bodies.
    print(
        "::warning::External assessment diagnostic raw save failed; typed stage unchanged",
        file=sys.stderr,
    )


def _cohort_report(
    plan: RunPlan,
    stage: CohortStage,
    stop_reason: str | None,
    started: float,
    timings: dict[str, float],
    provider_requests: int,
) -> dict[str, object]:
    report = summarize(plan, [stage.result])
    report.update(
        cohort_index=stage.result.cohort_index,
        stop_reason=stop_reason,
        http_requests=stage.http_requests,
        provider_requests=provider_requests,
        response_content_bytes=stage.response_content_bytes,
        response_bytes_basis="uncompressed fully received bodies; wire bytes unmeasured",
        cohort_complete=stage.result.complete,
        elapsed_seconds=round(time.monotonic() - started, 3),
        **{key: round(value, 3) for key, value in timings.items()},
    )
    return report


def acquire_cohort(
    store: ObjectStore,
    plan: RunPlan,
    index: int,
    *,
    soft_budget_seconds: float = SOFT_BUDGET_SECONDS,
) -> dict[str, object]:
    """Collect missing pairs locally, commit confirmed outcomes once, then upload raw.

    The workflow serializes acquisition. A hard kill before commit may repeat GETs;
    a saved success or failure is immutable and is never fetched again in this plan.
    """
    started = time.monotonic()
    if not 0 < soft_budget_seconds <= SOFT_BUDGET_SECONDS:
        raise ValueError("invalid external assessment soft budget")
    timings = {"collection_elapsed_seconds": 0.0, "storage_elapsed_seconds": 0.0}
    terminal = read_terminal(store, plan)
    if terminal is not None:
        return {**_terminal_result(terminal), "provider_requests": 0, "http_requests": 0}
    previous = read_stage(store, plan, index)
    stage = previous[0] if previous else CohortStage(new_result(plan, index))
    timings["storage_elapsed_seconds"] = time.monotonic() - started
    if stage.result.complete:
        return _cohort_report(plan, stage, None, started, timings, 0)
    require_binding(plan)
    original = stage
    provider_requests = 0
    stop_reason = None
    rate_limiter = ExternalAssessmentHostRateLimiter()
    with ExitStack() as resources:
        bundle = None
        try:
            bundle = resources.enter_context(RawBundle(plan.run_id, index))
        except Exception:
            _raw_warning()
        session = resources.enter_context(requests.Session())
        for entry in original.result.entries:
            if entry.state != "unattempted":
                continue
            if time.monotonic() - started + PAIR_AND_STAGE_RESERVE_SECONDS >= soft_budget_seconds:
                stop_reason = "soft_budget"
                break
            final_result: ExternalAssessmentCollectionResult | None = None
            for retry in range(MAX_PAIR_RETRIES + 1):
                received_html: list[ExternalAssessmentReceivedHTML] = []
                session.cookies.clear()
                collection_started = time.monotonic()
                result = external_assessment_collect_pair(
                    entry.ticker,
                    session=session,
                    source=entry.source,
                    on_received_html=received_html.append,
                    rate_limiter=rate_limiter,
                )
                timings["collection_elapsed_seconds"] += time.monotonic() - collection_started
                if len(result.outcomes) != 1 or len(result.observations) > 1:
                    raise ValueError("external assessment pair result shape changed")
                provider_requests += result.requests
                stage = replace(
                    stage,
                    http_requests=stage.http_requests + result.requests,
                    response_content_bytes=stage.response_content_bytes
                    + (result.response_content_bytes or 0),
                )
                # At most two bodies per attempt stay in memory. Disk/compression/R2
                # work is outside the finite pair deadline and cannot alter its result.
                storage_started = time.monotonic()
                if bundle is not None:
                    for received in received_html:
                        try:
                            bundle.append(entry.source, entry.ticker, received)
                        except Exception:
                            _raw_warning()
                timings["storage_elapsed_seconds"] += time.monotonic() - storage_started
                if result.outcomes[0].status == "not_attempted":
                    stop_reason = result.stop_reason or "soft_budget"
                    break
                final_result = result
                if result.outcomes[0].status != "rate_limited":
                    stop_reason = result.stop_reason
                    break
                if result.retry_after_unrepresentable:
                    stop_reason = "retry_after_unrepresentable"
                    break
                if retry == MAX_PAIR_RETRIES:
                    stop_reason = "rate_limited_retry_exhausted"
                    break
                finished = result.last_response_finished_at_utc
                if finished is None:
                    raise ValueError("confirmed HTTP 429 requires a received response time")
                deadline = result.retry_after_not_before_utc or (
                    finished
                    + timedelta(
                        seconds=RETRY_BACKOFF_SECONDS[retry] + secrets.randbelow(1001) / 1000
                    )
                )
                if not _wait_for_retry(deadline, started=started, budget=soft_budget_seconds):
                    stop_reason = "retry_soft_budget"
                    break
            if final_result is not None:
                stage = replace(
                    stage,
                    result=record_outcome(
                        plan,
                        stage.result,
                        final_result.outcomes[0],
                        final_result.observations[0] if final_result.observations else None,
                    ),
                )
            if stop_reason:
                break
        storage_started = time.monotonic()
        if stage.result != original.result:
            bundle_key = bundle.key if bundle is not None and bundle.receipts else None
            if bundle_key is not None:
                stage = replace(stage, raw_bundle_keys=(*stage.raw_bundle_keys, bundle_key))
            stage = commit_stage(store, plan, stage, previous=previous)
            if bundle_key is not None and bundle_key in stage.raw_bundle_keys:
                assert bundle is not None
                try:
                    bundle.finish()
                    bundle.upload(store)
                except Exception:
                    _raw_warning()
        timings["storage_elapsed_seconds"] += time.monotonic() - storage_started
    return _cohort_report(plan, stage, stop_reason, started, timings, provider_requests)


def write_outputs(
    plan: RunPlan | None,
    terminal: dict[str, object] | None = None,
    *,
    cohort_indices: tuple[int, ...] | None = None,
    run_id: str | None = None,
) -> None:
    values = {
        "run_id": plan.run_id if plan is not None else run_id or "",
        "eligible": "true",
        "exists": str(plan is not None).lower(),
        "cohorts": (
            json.dumps(
                list(cohort_indices)
                if cohort_indices is not None
                else [cohort.index for cohort in plan.cohorts]
            )
            if plan and terminal is None
            else "[]"
        ),
        "terminal_status": terminal_status(terminal) if terminal is not None else "pending",
    }
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8") as stream:
            for key, value in values.items():
                print(f"{key}={value}", file=stream)
    if terminal is not None:
        write_publication_outputs(_terminal_result(terminal))


def write_publication_outputs(report: dict[str, object]) -> None:
    """Expose only measured aggregates; normal_null means all metric values null."""
    states = report.get("state_counts")
    states = states if isinstance(states, dict) else {}
    counters = {
        "expected_pairs": report.get("expected_pairs"),
        "typed_pairs": report.get("observations"),
        "known_failure_pairs": states.get("failure"),
        "normal_null_pairs": states.get("normal_null"),
        "unattempted_pairs": states.get("unattempted"),
        "rows_added": report.get("rows_added"),
    }
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8") as stream:
            for key, value in counters.items():
                safe = str(value) if type(value) is int and value >= 0 else "unknown"
                print(f"external_assessment_{key}={safe}", file=stream)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("status", "plan", "acquire", "audit", "publish", "publish-confirmed-partial"),
    )
    parser.add_argument("--run-id")
    parser.add_argument("--scheduled", action="store_true")
    parser.add_argument("--run-created-at", type=datetime.fromisoformat)
    parser.add_argument("--master-asof", type=date.fromisoformat)
    parser.add_argument("--cohort", type=int)
    parser.add_argument("--db", type=Path, default=MARKET_DB_PATH)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--lease-handle", type=Path)
    parser.add_argument("--require-existing-plan", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan: RunPlan | None
        report: dict[str, object]
        observation_date = None
        if args.scheduled:
            if (
                args.command not in {"status", "plan"}
                or args.run_created_at is None
                or args.run_id is not None
                or args.master_asof is not None
                or args.require_existing_plan
            ):
                raise ValueError("scheduled plan/status requires only the original run clock")
            observation_date, eligible = external_assessment_target_date(
                args.run_created_at, datetime.now(UTC)
            )
            args.run_id = f"{SCHEDULED_BATCH_PREFIX}{observation_date.isoformat()}"
            if not eligible:
                if output := os.environ.get("GITHUB_OUTPUT"):
                    with Path(output).open("a", encoding="utf-8") as stream:
                        print("eligible=false", file=stream)
                print(json.dumps({"status": "before_close", "run_id": args.run_id}))
                return 0
        elif args.run_id is None or args.run_created_at is not None:
            raise ValueError("manual external assessment commands require a stable run id")
        store = Boto3R2Store(
            bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores",
            verify_put_with_head=args.command != "acquire",
        )
        if args.command == "plan":
            if args.master_asof is None and observation_date is None:
                raise ValueError("saved master date required")
            plan = initialize(
                store,
                run_id=args.run_id,
                master_asof=args.master_asof,
                mirror=args.directory,
                require_existing=args.require_existing_plan,
                observation_date=observation_date,
            )
        else:
            plan = read_plan(store, args.run_id)
        if args.command == "status":
            write_outputs(
                plan, read_terminal(store, plan) if plan is not None else None, run_id=args.run_id
            )
            print(json.dumps({"plan_exists": plan is not None}))
            return 0
        if plan is None:
            raise ValueError("external assessment fixed plan is missing")
        if args.command == "plan":
            terminal = read_terminal(store, plan)
            saved = (
                read_stages(store, plan)
                if terminal is None and not args.require_existing_plan
                else {}
            )
            cohorts = tuple(
                cohort.index
                for cohort in plan.cohorts
                if not args.require_existing_plan
                and (
                    cohort.index not in saved
                    or any(
                        entry.state == "unattempted"
                        for entry in saved[cohort.index][0].result.entries
                    )
                )
            )
            if terminal is None and cohorts:
                require_binding(plan)
            write_outputs(plan, terminal, cohort_indices=cohorts)
            report = (
                _terminal_result(terminal)
                if terminal is not None
                else {
                    "tickers": len(plan.tickers),
                    "cohorts": len(plan.cohorts),
                    "expected_pairs": len(plan.tickers) * len(plan.sources),
                }
            )
        elif args.command == "acquire":
            if args.cohort is None:
                raise ValueError("cohort required")
            report = acquire_cohort(store, plan, args.cohort)
        elif args.command == "audit":
            stages = read_stages(store, plan)
            report = summarize(plan, [stage.result for stage, _ in stages.values()])
        else:
            terminal = read_terminal(store, plan)
            if terminal is not None:
                report = _terminal_result(terminal)
            else:
                if args.directory is None or args.lease_handle is None:
                    raise ValueError("private publish directory and lease handle required")
                from baibai_batch.jobs.external_assessment_publication import publish_and_verify
                from baibai_batch.storage.publication_lease import _load_handle

                report = publish_and_verify(
                    store,
                    plan,
                    handle=_load_handle(args.lease_handle),
                    sqlite_path=args.db,
                    mirror=args.db.parents[1],
                    directory=args.directory,
                    confirmed_partial=args.command == "publish-confirmed-partial",
                )
            write_publication_outputs(report)
        print(json.dumps(report, sort_keys=True))
        if args.command == "acquire":
            if report.get("status") in {"already_published", "already_partially_published"}:
                return 0
            if not report["cohort_complete"] or report["stop_reason"]:
                return 1
        return 0
    except Exception as error:
        print(f"External assessment daily failed; type={type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
