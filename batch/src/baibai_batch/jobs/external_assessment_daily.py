"""Daily and manual fixed-cohort collection through the same private handoff."""

from __future__ import annotations

import argparse
import hashlib
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
from baibai_batch.storage.external_assessment_raw import RawBundle
from baibai_batch.storage.external_assessment_staging import (
    CohortStage,
    commit_stage,
    read_plan,
    read_stage,
    read_stages,
    read_terminal,
    save_new_plan,
    terminal_result,
)
from baibai_batch.storage.source_dependencies import (
    available_dependencies, latest_dependency, load_dependency,
)
from baibai_batch.storage.lake_publish import Boto3R2Store, ObjectStore
from baibai_batch.storage.publish_market_lake import _resolve_serving_release, _serving_pointer
from baibai_engine.batch_api import (
    EquityWebSourceResponse,
    ExternalAssessmentCollectionResult,
    ExternalAssessmentHostRateLimiter,
    ExternalAssessmentSource,
    LakeObjectCache,
    external_assessment_calendar_master_asof,
    external_assessment_collect_pair,
    external_assessment_collector_binding,
    external_assessment_frozen_lake_universe,
    external_assessment_received_calendar,
    external_assessment_received_master_universe,
    lake_session,
)

SOFT_BUDGET_SECONDS = 4 * 60 * 60
PAIR_AND_STAGE_RESERVE_SECONDS = 15 * 60
MAX_PAIR_RETRIES = 5
RETRY_BACKOFF_SECONDS = (5, 10, 20, 40, 80)


def require_binding(plan: RunPlan) -> None:
    if plan.collector_binding != external_assessment_collector_binding(
        source=plan.sources[0] if len(plan.sources) == 1 else None
    ):
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
    source: ExternalAssessmentSource | None = None,
) -> RunPlan:
    if (master_asof is None) == (observation_date is None):
        raise ValueError(
            "external assessment plan requires either a saved master or a scheduled date"
        )
    existing = read_plan(store, run_id)
    if existing is not None:
        if source is not None and existing.sources != (source,):
            raise ValueError("saved plan source differs; old multi-source plans cannot be reused")
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
        try:
            if observation_date is not None:
                master_asof = external_assessment_calendar_master_asof(
                    session, release=release, cache=cache, observation_date=observation_date
                )
            assert master_asof is not None
            tickers, binding = external_assessment_frozen_lake_universe(
                session, release=release, cache=cache, master_asof=master_asof,
            )
        except ValueError:
            # A new plan may start before the independently scheduled integration.
            # Freeze only native master/calendar dependencies, never the market DB.
            references = available_dependencies(store)
            calendar = None
            if observation_date is not None:
                calendar = latest_dependency(references, source="market_calendar", day=observation_date)
                overrides = external_assessment_received_calendar(load_dependency(store, calendar)) if calendar else None
                master_asof = external_assessment_calendar_master_asof(
                    session, release=release, cache=cache, observation_date=observation_date,
                    overrides=overrides,
                )
            assert master_asof is not None
            master = latest_dependency(references, source="master", day=master_asof)
            if master is None:
                raise ValueError("exact master is unavailable in the lake and ready dependencies") from None
            tickers = external_assessment_received_master_universe(load_dependency(store, master), master_asof)
            binding = json.dumps({
                "master_asof": master_asof.isoformat(),
                "base_release_id": release.release_id,
                "base_release_manifest_sha256": release.manifest_sha256,
                "master_receipt": master,
                "calendar_receipt": calendar,
                "tickers_sha256": hashlib.sha256("\n".join(tickers).encode()).hexdigest(),
            }, sort_keys=True, separators=(",", ":"))
    plan = make_plan(
        run_id=run_id,
        tickers=tickers,
        universe_binding=binding,
        collector_binding=external_assessment_collector_binding(source=source),
        sources=(source,) if source is not None else ("monex", "rakuten", "kabumap", "minkabu"),
    )
    if len(plan.cohorts) > 256:
        raise ValueError("external assessment cohort count exceeds the supported Actions matrix")
    save_new_plan(store, plan)
    return plan


def _wait_for_retry(deadline: datetime, *, started: float, budget: float) -> bool:
    while True:
        waiting = max(0.0, (deadline - datetime.now(UTC)).total_seconds())
        if time.monotonic() - started + waiting + PAIR_AND_STAGE_RESERVE_SECONDS >= budget:
            return False
        if waiting == 0:
            return True
        time.sleep(min(waiting, 30.0))


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
    session: requests.Session | None = None,
    rate_limiter: ExternalAssessmentHostRateLimiter | None = None,
) -> dict[str, object]:
    """Collect missing pairs, upload captured originals, then commit confirmed outcomes.

    The workflow serializes acquisition. A hard kill before commit may repeat GETs;
    a saved success or failure is immutable and is never fetched again in this plan.
    """
    started = time.monotonic()
    if not 0 < soft_budget_seconds <= SOFT_BUDGET_SECONDS:
        raise ValueError("invalid external assessment soft budget")
    timings = {"collection_elapsed_seconds": 0.0, "storage_elapsed_seconds": 0.0}
    terminal = read_terminal(store, plan)
    if terminal is not None:
        return {**terminal_result(terminal), "provider_requests": 0, "http_requests": 0}
    previous = read_stage(store, plan, index)
    stage = previous[0] if previous else CohortStage(new_result(plan, index))
    timings["storage_elapsed_seconds"] = time.monotonic() - started
    if stage.result.complete:
        return _cohort_report(plan, stage, None, started, timings, 0)
    require_binding(plan)
    original = stage
    provider_requests = 0
    stop_reason = None
    rate_limiter = rate_limiter or ExternalAssessmentHostRateLimiter()
    with ExitStack() as resources:
        bundle = resources.enter_context(RawBundle(plan.run_id, index))
        session = session if session is not None else resources.enter_context(requests.Session())
        # Rotate hosts while keeping the saved source-major entry order unchanged.
        # Stable sorting retains the plan's source order within each ticker.
        for entry in sorted(original.result.entries, key=lambda entry: entry.ticker):
            if entry.state != "unattempted":
                continue
            if time.monotonic() - started + PAIR_AND_STAGE_RESERVE_SECONDS >= soft_budget_seconds:
                stop_reason = "soft_budget"
                break
            final_result: ExternalAssessmentCollectionResult | None = None
            for retry in range(MAX_PAIR_RETRIES + 1):
                received_html: list[EquityWebSourceResponse] = []
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
                for received in received_html:
                    bundle.append(received)
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
            for entry in stage.result.entries:
                if entry.state in {"success", "normal_null"} and entry != next(
                    prior
                    for prior in original.result.entries
                    if (prior.source, prior.ticker) == (entry.source, entry.ticker)
                ):
                    receipts = [
                        receipt
                        for receipt in bundle.receipts
                        if receipt["attempt_id"] == entry.attempt_id
                    ]
                    expected_roles = {"page", "iframe"} if entry.source == "rakuten" else {"page"}
                    if {receipt["role"] for receipt in receipts} != expected_roles or any(
                        (receipt["source"], receipt["requested_ticker"])
                        != (entry.source, entry.ticker)
                        for receipt in receipts
                    ):
                        raise ValueError("typed pair has no complete captured original attempt")
            bundle_key = bundle.key if bundle.receipts else None
            if bundle_key is not None:
                bundle.finish()
                bundle.upload(store)
                stage = replace(stage, raw_bundle_keys=(*stage.raw_bundle_keys, bundle_key))
            stage = commit_stage(store, plan, stage, previous=previous)
        timings["storage_elapsed_seconds"] += time.monotonic() - storage_started
    return _cohort_report(plan, stage, stop_reason, started, timings, provider_requests)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only fixed-plan history and audit")
    parser.add_argument("command", choices=("status", "audit", "terminal"))
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    try:
        store = Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
        plan = read_plan(store, args.run_id)
        if args.command == "status":
            report: dict[str, object] = {"plan_exists": plan is not None}
        elif plan is None:
            raise ValueError("external assessment fixed plan is missing")
        elif args.command == "audit":
            report = summarize(
                plan, [stage.result for stage, _ in read_stages(store, plan).values()]
            )
        else:
            terminal = read_terminal(store, plan)
            if terminal is None:
                raise ValueError("external assessment terminal is missing")
            report = terminal_result(terminal)
        print(json.dumps(report, sort_keys=True))
        return 0
    except Exception as error:
        print(f"External assessment history failed; type={type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
