"""Daily and manual fixed-cohort collection through the same private handoff."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from baibai_batch.jobs.retail_cohorts import (
    RunPlan,
    begin_rate_limited_retry,
    make_plan,
    mark_inflight,
    new_journal,
    record_outcome,
    summarize,
    validate_resume,
)
from baibai_batch.jobs.schedule import retail_target_date
from baibai_batch.storage.lake_publish import Boto3R2Store, ObjectStore
from baibai_batch.storage.publish_market_lake import _resolve_serving_release, _serving_pointer
from baibai_batch.storage.retail_staging import (
    MAX_PAIR_RETRIES,
    Checkpoint,
    PairRetryState,
    checkpoint_bytes,
    checkpoint_key,
    put_private,
    read_checkpoints,
    read_plan,
    read_terminal,
    save_new_plan,
    terminal_status,
)
from baibai_engine.batch_api import (
    MARKET_DB_PATH,
    LakeObjectCache,
    RetailCollectionResult,
    RetailHostRateLimiter,
    RetailSource,
    lake_session,
    retail_calendar_master_asof,
    retail_collect_pair,
    retail_collector_binding,
    retail_frozen_lake_universe,
)

SOFT_BUDGET_SECONDS = 4 * 60 * 60
PAIR_AND_CHECKPOINT_RESERVE_SECONDS = 15 * 60
RETRY_BACKOFF_SECONDS = (5, 10, 20, 40, 80)


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
    store: ObjectStore,
    *,
    run_id: str,
    master_asof: date | None,
    mirror: Path | None,
    require_existing: bool = False,
    observation_date: date | None = None,
) -> RunPlan:
    if (master_asof is None) == (observation_date is None):
        raise ValueError("retail plan requires either a saved master or a scheduled date")
    existing = read_plan(store, run_id)
    if existing is not None:
        saved_master = date.fromisoformat(json.loads(existing.universe_binding)["master_asof"])
        if master_asof is not None and saved_master != master_asof:
            raise ValueError("retail saved master assertion changed")
        if observation_date is not None and saved_master > observation_date:
            raise ValueError("retail saved master is after the scheduled date")
        return existing
    if require_existing:
        raise ValueError("retail partial publication requires an existing fixed plan")
    if mirror is None:
        raise ValueError("private master cache directory required for a new plan")
    mirror.mkdir(mode=0o700, parents=True, exist_ok=True)
    mirror.chmod(0o700)
    pointer = _serving_pointer(store).pointer
    if pointer is None:
        raise ValueError("retail plan requires an existing L1 release")
    release = _resolve_serving_release(store, mirror, pointer)
    with lake_session() as session:
        cache = LakeObjectCache(root=mirror, source=_MasterSource(store))
        if observation_date is not None:
            master_asof = retail_calendar_master_asof(
                session, release=release, cache=cache, observation_date=observation_date
            )
        assert master_asof is not None
        tickers, binding = retail_frozen_lake_universe(
            session,
            release=release,
            cache=cache,
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


def _retry_states(checkpoint: Checkpoint, retry: PairRetryState) -> tuple[PairRetryState, ...]:
    return tuple(
        sorted(
            (
                *[
                    item
                    for item in checkpoint.retry_states
                    if (item.source, item.ticker) != (retry.source, retry.ticker)
                ],
                retry,
            ),
            key=lambda item: (item.source, item.ticker),
        )
    )


def _pending_retry(
    plan: RunPlan, saved: dict[int, tuple[Checkpoint, str]]
) -> tuple[int, PairRetryState] | None:
    pending = [
        (index, retry)
        for index, (checkpoint, _) in saved.items()
        for retry in checkpoint.retry_states
        if retry.next_retry_utc is not None
        and retry.retries_used < MAX_PAIR_RETRIES
        and not retry.retry_after_unrepresentable
    ]
    if len(pending) > 1:
        raise ValueError("multiple pending retry pairs violate serial acquisition")
    validate_resume(
        plan,
        [checkpoint.journal for checkpoint, _ in saved.values()],
        retryable_pairs=frozenset((retry.source, retry.ticker) for _, retry in pending),
    )
    return pending[0] if pending else None


def _wait_for_retry(deadline: datetime, *, started: float, budget: float) -> bool:
    while True:
        waiting = max(0.0, (deadline - datetime.now(UTC)).total_seconds())
        if time.monotonic() - started + waiting + PAIR_AND_CHECKPOINT_RESERVE_SECONDS >= budget:
            return False
        if waiting == 0:
            return True
        time.sleep(min(waiting, 30.0))


def _cohort_report(
    plan: RunPlan, checkpoint: Checkpoint, index: int, stop_reason: str | None
) -> dict[str, object]:
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


def _acquire_pair(
    store: ObjectStore,
    plan: RunPlan,
    checkpoint: Checkpoint,
    etag: str | None,
    source: RetailSource,
    ticker: str,
    rate_limiter: RetailHostRateLimiter,
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
            retry = checkpoint.retry_for(source, ticker)
            if retry is not None and retry.next_retry_utc is not None:
                if retry.retries_used >= MAX_PAIR_RETRIES or retry.next_retry_utc > datetime.now(
                    UTC
                ):
                    raise ValueError("retry reservation before deadline or beyond retry limit")
                checkpoint = replace(
                    checkpoint,
                    journal=begin_rate_limited_retry(plan, checkpoint.journal, source, ticker),
                    retry_states=_retry_states(
                        checkpoint,
                        replace(retry, retries_used=retry.retries_used + 1, next_retry_utc=None),
                    ),
                )
            else:
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
        retry = checkpoint.retry_for(source, ticker)
        if not marked and retry is not None and result.outcomes[0].status == "not_attempted":
            return
        retries = checkpoint.retry_states
        if result.outcomes[0].status == "rate_limited":
            finished = result.last_response_finished_at_utc
            if finished is None:
                raise ValueError("confirmed HTTP 429 requires a received response time")
            used = retry.retries_used if retry else 0
            deadline = None
            if used < MAX_PAIR_RETRIES and not result.retry_after_unrepresentable:
                deadline = result.retry_after_not_before_utc or (
                    finished
                    + timedelta(
                        seconds=RETRY_BACKOFF_SECONDS[used] + secrets.randbelow(1001) / 1000
                    )
                )
            retries = _retry_states(
                checkpoint,
                PairRetryState(source, ticker, used, deadline, result.retry_after_unrepresentable),
            )
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
            retry_states=retries,
        )
        persist()

    result = retail_collect_pair(
        ticker,
        source=source,
        before_request=before_request,
        on_response=on_response,
        on_outcome=on_outcome,
        rate_limiter=rate_limiter,
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
    key = checkpoint_key(plan, index)
    terminal = read_terminal(store, plan)
    if terminal is not None:
        return {
            **_terminal_result(terminal),
            "confirmed_http_requests": 0,
            "request_reservations": 0,
        }
    saved = read_checkpoints(store, plan)
    pending = _pending_retry(plan, saved)
    checkpoint, etag = saved.get(index, (Checkpoint(new_journal(plan, index)), None))
    if pending is not None and pending[0] != index:
        if all(
            entry.state not in {"unattempted", "uncertain"} for entry in checkpoint.journal.entries
        ):
            return _cohort_report(plan, checkpoint, index, None)
        raise ValueError("another cohort has a pending provider retry")
    stop_reason = None
    rate_limiter = RetailHostRateLimiter()

    entries = tuple(checkpoint.journal.entries)
    if pending is not None:
        retry_pair = (pending[1].source, pending[1].ticker)
        entries = tuple(
            sorted(entries, key=lambda entry: (entry.source, entry.ticker) != retry_pair)
        )
    for entry in entries:
        retry = checkpoint.retry_for(entry.source, entry.ticker)
        if entry.state != "unattempted" and not (
            retry is not None and retry.next_retry_utc is not None
        ):
            continue
        require_binding(plan)
        # Leave a normal completed checkpoint, rather than rely on runner killing
        # a request. The pair's own real timer also includes its durable callbacks.
        if time.monotonic() - started + PAIR_AND_CHECKPOINT_RESERVE_SECONDS >= soft_budget_seconds:
            stop_reason = "soft_budget"
            break
        while True:
            retry = checkpoint.retry_for(entry.source, entry.ticker)
            if (
                retry is not None
                and retry.next_retry_utc is not None
                and not _wait_for_retry(
                    retry.next_retry_utc, started=started, budget=soft_budget_seconds
                )
            ):
                stop_reason = "retry_pending_soft_budget"
                break
            checkpoint, etag, result = _acquire_pair(
                store,
                plan,
                checkpoint,
                etag,
                entry.source,
                entry.ticker,
                rate_limiter,
            )
            if result.stop_reason == "rate_limited":
                retry = checkpoint.retry_for(entry.source, entry.ticker)
                if retry is not None and retry.next_retry_utc is not None:
                    continue
                stop_reason = (
                    "retry_after_unrepresentable"
                    if retry and retry.retry_after_unrepresentable
                    else "rate_limited_retry_exhausted"
                )
            elif result.stop_reason:
                stop_reason = result.stop_reason
            break
        if stop_reason:
            break
    # Even an untouched soft-stop cohort remains explicit, rather than disappearing.
    if etag is None:
        put_private(store, key, checkpoint_bytes(plan, checkpoint), previous_etag=None)
    return _cohort_report(plan, checkpoint, index, stop_reason)


def write_outputs(
    plan: RunPlan | None,
    terminal: dict[str, object] | None = None,
    *,
    retry_cohort: int | None = None,
    cohort_indices: tuple[int, ...] | None = None,
    run_id: str | None = None,
) -> None:
    values = {
        "run_id": plan.run_id if plan is not None else run_id or "",
        "eligible": "true",
        "exists": str(plan is not None).lower(),
        "cohorts": (
            json.dumps(
                [retry_cohort]
                if retry_cohort is not None
                else list(cohort_indices)
                if cohort_indices is not None
                else [cohort.index for cohort in plan.cohorts]
            )
            if plan and terminal is None
            else "[]"
        ),
        "terminal_status": terminal_status(terminal) if terminal is not None else "pending",
        "retry_only": str(retry_cohort is not None and terminal is None).lower(),
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
        "uncertain_pairs": states.get("uncertain"),
        "unattempted_pairs": states.get("unattempted"),
        "rows_added": report.get("rows_added"),
    }
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8") as stream:
            for key, value in counters.items():
                safe = str(value) if type(value) is int and value >= 0 else "unknown"
                print(f"retail_{key}={safe}", file=stream)


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
            observation_date, eligible = retail_target_date(args.run_created_at, datetime.now(UTC))
            args.run_id = f"retail-daily-{observation_date.isoformat()}"
            if not eligible:
                if output := os.environ.get("GITHUB_OUTPUT"):
                    with Path(output).open("a", encoding="utf-8") as stream:
                        print("eligible=false", file=stream)
                print(json.dumps({"status": "before_close", "run_id": args.run_id}))
                return 0
        elif args.run_id is None or args.run_created_at is not None:
            raise ValueError("manual retail commands require a stable run id")
        store = Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
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
            raise ValueError("retail fixed plan is missing")
        if args.command == "plan":
            terminal = read_terminal(store, plan)
            saved = (
                read_checkpoints(store, plan)
                if terminal is None and not args.require_existing_plan
                else {}
            )
            pending = _pending_retry(plan, saved) if saved else None
            cohorts = tuple(
                cohort.index
                for cohort in plan.cohorts
                if not args.require_existing_plan
                and (
                    cohort.index not in saved
                    or any(
                        entry.state == "unattempted"
                        for entry in saved[cohort.index][0].journal.entries
                    )
                )
            )
            if terminal is None and (pending is not None or cohorts):
                require_binding(plan)
            write_outputs(
                plan, terminal, retry_cohort=pending[0] if pending else None, cohort_indices=cohorts
            )
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
            checkpoints = read_checkpoints(store, plan)
            report = summarize(plan, [checkpoint.journal for checkpoint, _ in checkpoints.values()])
        else:
            terminal = read_terminal(store, plan)
            if terminal is not None:
                report = _terminal_result(terminal)
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
                    confirmed_partial=args.command == "publish-confirmed-partial",
                )
            write_publication_outputs(report)
        print(json.dumps(report, sort_keys=True))
        if args.command == "acquire":
            if report.get("status") in {"already_published", "already_partially_published"}:
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
