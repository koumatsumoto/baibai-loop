"""Source-isolated acquisition and immutable handoff to common integration."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import requests

import baibai_jobs.acquisition.external_assessments.daily as daily
from baibai_engine.data_collection.receipts.public_pages.models import (
    Source as ExternalAssessmentSource,
)
from baibai_engine.data_collection.transport.public_pages import (
    HostRateLimiter as ExternalAssessmentHostRateLimiter,
)
from baibai_engine.storage.objects.r2 import (
    Boto3R2Store,
    ObjectStore,
)
from baibai_jobs.acquisition.external_assessments.cohorts import RunPlan, summarize
from baibai_jobs.acquisition.external_assessments.identity import SCHEDULED_BATCH_PREFIX
from baibai_jobs.acquisition.external_assessments.staging import (
    prefix,
    read_plan,
    read_stages,
    read_terminal,
    terminal_result,
)
from baibai_jobs.integration.external_assessment_ready import PreparedPlan, prepare_plan
from baibai_jobs.schedule import external_assessment_target_date

SOURCES: tuple[ExternalAssessmentSource, ...] = ("monex", "rakuten", "kabumap", "minkabu")


def require_source(plan: RunPlan, source: str) -> None:
    if source not in SOURCES or plan.sources != (source,):
        raise ValueError(
            "source mismatch; old multi-source plans require explicit disposition, never conversion"
        )


def selection(
    *, source: str, batch_id: str | None, created_at: datetime | None
) -> tuple[list[dict[str, str]], date | None, bool]:
    if source not in (*SOURCES, "all"):
        raise ValueError("unknown source")
    observation_date = None
    eligible = True
    if created_at is not None:
        if batch_id:
            raise ValueError("scheduled identity cannot override the original run clock")
        observation_date, eligible = external_assessment_target_date(created_at, datetime.now(UTC))
    elif not batch_id:
        raise ValueError("manual source acquisition requires a stable batch label")
    exact_scheduled_id = bool(batch_id and batch_id.startswith(SCHEDULED_BATCH_PREFIX))
    if exact_scheduled_id:
        expected = f"{SCHEDULED_BATCH_PREFIX}{source}-"
        if source == "all" or not cast("str", batch_id).startswith(expected):
            raise ValueError("existing scheduled identity requires its one exact source")
        suffix = cast("str", batch_id)[len(expected) :]
        if date.fromisoformat(suffix).isoformat() != suffix:
            raise ValueError("existing scheduled identity requires a canonical date")
    selected = SOURCES if source == "all" else (cast("ExternalAssessmentSource", source),)
    result = []
    for name in selected:
        run_id = (
            f"{SCHEDULED_BATCH_PREFIX}{name}-{observation_date.isoformat()}"
            if observation_date
            else cast("str", batch_id)
            if exact_scheduled_id
            else f"{name}-{batch_id}"
        )
        prefix(run_id)  # Validate the whole namespaced ID before any credential use.
        result.append({"source": name, "run_id": run_id})
    return result, observation_date, eligible


def read_selection(payload: str) -> list[dict[str, str]]:
    value = json.loads(payload)
    if not isinstance(value, list) or not 1 <= len(value) <= len(SOURCES):
        raise ValueError("invalid exact source selection")
    result: list[dict[str, str]] = []
    for entry in value:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"source", "run_id"}
            or any(type(v) is not str for v in entry.values())
        ):
            raise ValueError("invalid source selection entry")
        name, run_id = entry["source"], entry["run_id"]
        if name not in SOURCES or not (
            run_id.startswith((f"{name}-", f"{SCHEDULED_BATCH_PREFIX}{name}-"))
        ):
            raise ValueError("source identity is not namespaced")
        prefix(run_id)
        if any(item["source"] == name or item["run_id"] == run_id for item in result):
            raise ValueError("duplicate source selection")
        result.append({"source": name, "run_id": run_id})
    return result


def acquire_source(
    store: ObjectStore, plan: RunPlan, *, source: str, budget: float = daily.SOFT_BUDGET_SECONDS
) -> dict[str, object]:
    require_source(plan, source)
    if not 0 < budget <= daily.SOFT_BUDGET_SECONDS:
        raise ValueError("invalid source acquisition budget")
    terminal = read_terminal(store, plan)
    if terminal is not None:
        return {**terminal_result(terminal), "source": source, "provider_requests": 0}
    daily.require_binding(plan)
    started = time.monotonic()
    saved = read_stages(store, plan)
    requests_made = 0
    stop_reason = None
    limiter = ExternalAssessmentHostRateLimiter()
    with requests.Session() as session:
        for cohort in plan.cohorts:
            previous = saved.get(cohort.index)
            if previous is not None and previous[0].result.complete:
                continue
            remaining = budget - (time.monotonic() - started)
            if remaining <= daily.PAIR_AND_STAGE_RESERVE_SECONDS:
                stop_reason = "soft_budget"
                break
            report = daily.acquire_cohort(
                store,
                plan,
                cohort.index,
                soft_budget_seconds=remaining,
                session=session,
                rate_limiter=limiter,
            )
            requests_made += cast("int", report["provider_requests"])
            if report.get("stop_reason") or report.get("cohort_complete") is not True:
                stop_reason = cast("str", report.get("stop_reason") or "incomplete")
                break
    saved = read_stages(store, plan)
    results = [stage.result for stage, _ in saved.values()]
    accounting = summarize(plan, results)
    blocked = any(entry.http_status in {403, 429} for result in results for entry in result.entries)
    return {
        **accounting,
        "source": source,
        "status": "blocked"
        if blocked
        else "acquired"
        if accounting["attempted_completeness"]
        else "incomplete",
        "stop_reason": stop_reason,
        "provider_requests": requests_made,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def inspect_ready(
    store: ObjectStore,
    selected: list[dict[str, str]],
    *,
    confirmed_partial: bool = False,
    master_asof: date | None = None,
) -> tuple[list[PreparedPlan], dict[str, dict[str, object]]]:
    """Read only exact handoffs; all-sealed resume needs no lease or hydrate."""
    if confirmed_partial and len(selected) != 1:
        raise ValueError("explicit partial publication requires one exact source plan")
    prepared: list[PreparedPlan] = []
    reports: dict[str, dict[str, object]] = {}
    for entry in selected:
        source, run_id = entry["source"], entry["run_id"]
        plan = read_plan(store, run_id)
        if plan is None:
            if confirmed_partial:
                raise ValueError("explicit partial publication requires an existing source plan")
            reports[source] = {"source": source, "status": "not_started"}
            continue
        require_source(plan, source)
        if (
            master_asof is not None
            and json.loads(plan.universe_binding)["master_asof"] != master_asof.isoformat()
        ):
            raise ValueError("saved master assertion changed")
        terminal = read_terminal(store, plan)
        if terminal is not None:
            reports[source] = {**terminal_result(terminal), "source": source}
            continue
        saved = read_stages(store, plan)
        results = [stage.result for stage, _ in saved.values()]
        accounting = summarize(plan, results)
        blocked = any(
            item.http_status in {403, 429} for result in results for item in result.entries
        )
        if not confirmed_partial and (blocked or not accounting["attempted_completeness"]):
            reports[source] = {
                **accounting,
                "source": source,
                "status": "blocked" if blocked else "incomplete",
            }
            continue
        # Validate the exact stages before freezing a handoff; do not integrate here.
        prepared.append(prepare_plan(store, plan, confirmed_partial=confirmed_partial))
        reports[source] = {**accounting, "source": source, "status": "publication_unconfirmed"}
    return prepared, reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "acquire", "inspect", "ready"))
    parser.add_argument("--source", choices=(*SOURCES, "all"), default="all")
    parser.add_argument("--batch-id")
    parser.add_argument("--run-created-at", type=datetime.fromisoformat)
    parser.add_argument("--run-id")
    parser.add_argument("--selection-json")
    parser.add_argument("--master-asof", type=date.fromisoformat)
    parser.add_argument("--observation-date", type=date.fromisoformat)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--confirmed-partial", action="store_true")
    parser.add_argument("--report-output", type=Path)
    args = parser.parse_args(argv)
    try:
        report: dict[str, object]
        success = True
        if args.command == "prepare":
            selected, day, eligible = selection(
                source=args.source, batch_id=args.batch_id, created_at=args.run_created_at
            )
            if args.run_created_at is None and args.master_asof is None:
                raise ValueError("manual acquisition requires a saved master date")
            if args.run_created_at is not None and args.master_asof is not None:
                raise ValueError("scheduled plan uses the original observation date")
            if args.confirmed_partial and (len(selected) != 1 or args.run_created_at is not None):
                raise ValueError("partial publication is an explicit single-source operation")
            report = {
                "eligible": str(eligible).lower(),
                "source_runs": json.dumps(selected),
                "observation_date": day.isoformat() if day else "",
                "master_asof": args.master_asof.isoformat() if args.master_asof else "",
            }
            if output := os.environ.get("GITHUB_OUTPUT"):
                with Path(output).open("a", encoding="utf-8") as stream:
                    for key, value in report.items():
                        print(f"{key}={value}", file=stream)
        else:
            selected = (
                read_selection(args.selection_json)
                if args.command in {"inspect", "ready"}
                else read_selection(json.dumps([{"source": args.source, "run_id": args.run_id}]))
            )
            if args.command not in {"inspect", "ready"} and args.directory is None:
                raise ValueError("private work directory required")
            store = Boto3R2Store(
                bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores",
                verify_put_with_head=args.command != "acquire",
            )
            if args.command in {"inspect", "ready"}:
                ready, states = inspect_ready(
                    store,
                    selected,
                    confirmed_partial=args.confirmed_partial,
                    master_asof=args.master_asof,
                )
                references = []
                if args.command == "ready":
                    from baibai_jobs.integration.external_assessment_ready import save_ready

                    for item in ready:
                        references.append(save_ready(store, item))
                        states[item.plan.sources[0]]["status"] = "ready_for_integration"
                report = {
                    "sources": [states[entry["source"]] for entry in selected],
                    "ready_handoffs": references,
                }
                if output := os.environ.get("GITHUB_OUTPUT"):
                    with Path(output).open("a", encoding="utf-8") as stream:
                        print(f"ready={str(bool(ready)).lower()}", file=stream)
                success = bool(ready) or all(
                    row["status"] in {"already_published", "already_partially_published"}
                    for row in states.values()
                )
            else:
                source = cast("ExternalAssessmentSource", args.source)
                plan = daily.initialize(
                    store,
                    run_id=selected[0]["run_id"],
                    master_asof=args.master_asof,
                    observation_date=args.observation_date,
                    mirror=args.directory,
                    source=source,
                    require_existing=(
                        args.observation_date is None
                        and selected[0]["run_id"].startswith(SCHEDULED_BATCH_PREFIX)
                    ),
                )
                report = acquire_source(store, plan, source=source)
                success = report["status"] in {
                    "acquired",
                    "already_published",
                    "already_partially_published",
                }
        if args.report_output is not None:
            from baibai_engine.storage.json import write_json_atomic

            write_json_atomic(args.report_output, report)
        print(json.dumps(report, sort_keys=True))
        return 0 if success else 1
    except Exception as error:
        print(
            f"External assessment source operation failed; type={type(error).__name__}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
