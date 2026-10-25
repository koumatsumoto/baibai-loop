"""Freeze exact inspected cohort stages for the independently scheduled integration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from baibai_jobs.jobs.external_assessment_cohorts import RunPlan, summarize
from baibai_jobs.storage.external_assessment_staging import (
    CohortStage,
    load_stage,
    prefix,
    put_private,
    read_plan,
    read_private,
    read_stages,
    read_terminal,
    stage_bytes,
)
from baibai_jobs.storage.lake_publish import LakePublishError, ObjectStore
from baibai_jobs.storage.source_receipts import ReceiptStore


@dataclass(frozen=True)
class PreparedPlan:
    plan: RunPlan
    stages: dict[int, tuple[CohortStage, str]]
    accounting: dict[str, object]
    confirmed_partial: bool


def prepare_plan(
    store: ObjectStore, plan: RunPlan, *, confirmed_partial: bool = False
) -> PreparedPlan:
    """Validate one exact plan's handoff without importing or publishing anything."""
    return prepare_saved_stages(plan, read_stages(store, plan), confirmed_partial=confirmed_partial)


def prepare_saved_stages(
    plan: RunPlan, stages: dict[int, tuple[CohortStage, str]], *, confirmed_partial: bool = False
) -> PreparedPlan:
    """Apply the same exact/partial checks to an immutable stage selection."""
    results = [stage.result for stage, _ in stages.values()]
    if not confirmed_partial and any(
        entry.http_status in {403, 429} for result in results for entry in result.entries
    ):
        raise ValueError(
            "blocked provider response requires explicit confirmed partial publication"
        )
    accounting = summarize(plan, results, require_all=not confirmed_partial)
    if not confirmed_partial and not accounting["attempted_completeness"]:
        raise ValueError("external assessment publication requires all fixed pairs attempted")
    staged_observations = tuple(item for result in results for item in result.observations)
    if confirmed_partial and not staged_observations:
        raise ValueError(
            "external assessment partial publication requires confirmed typed observations"
        )
    return PreparedPlan(plan, stages, accounting, confirmed_partial)


def save_ready(store: ObjectStore, item: PreparedPlan) -> dict[str, str]:
    plan = item.plan
    stages = []
    for index, (stage, _) in sorted(item.stages.items()):
        payload = stage_bytes(plan, stage)
        digest = hashlib.sha256(payload).hexdigest()
        key = prefix(plan.run_id) + f"/confirmed-stages/{digest}.json"
        saved = read_private(store, key)
        if saved is None:
            put_private(store, key, payload, previous_etag=None)
        elif saved.payload != payload:
            raise LakePublishError("confirmed external stage identity conflicts")
        stages.append({"index": index, "key": key, "sha256": digest})
    value = {
        "run_id": plan.run_id,
        "source": plan.sources[0],
        "plan_sha256": plan.plan_sha256,
        "confirmed_partial": item.confirmed_partial,
        "stages": stages,
    }
    identity = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    key = prefix(plan.run_id) + f"/ready-{identity}.json"
    saved = read_private(store, key)
    if saved is None:
        value["created_at"] = datetime.now(UTC).isoformat()
        saved = put_private(
            store, key, json.dumps(value, sort_keys=True).encode(), previous_etag=None
        )
    else:
        previous = json.loads(saved.payload)
        previous.pop("created_at")
        if previous != value:
            raise ValueError("external ready identity has different stage content")
    return {"key": key, "sha256": hashlib.sha256(saved.payload).hexdigest()}


def load_ready(store: ObjectStore, reference: dict[str, str]) -> PreparedPlan:
    saved = read_private(store, reference["key"])
    if saved is None or hashlib.sha256(saved.payload).hexdigest() != reference["sha256"]:
        raise ValueError("fixed external ready selection changed")
    value = json.loads(saved.payload)
    plan = read_plan(store, value["run_id"])
    if (
        plan is None
        or plan.plan_sha256 != value["plan_sha256"]
        or plan.sources != (value["source"],)
    ):
        raise ValueError("external ready selection differs from its exact plan")
    stages = {}
    if type(value["confirmed_partial"]) is not bool:
        raise ValueError("external partial confirmation must be explicit")
    for stage_ref in value["stages"]:
        expected_key = prefix(plan.run_id) + f"/confirmed-stages/{stage_ref['sha256']}.json"
        if stage_ref["key"] != expected_key or stage_ref["index"] in stages:
            raise ValueError("invalid confirmed stage scope")
        saved = read_private(store, stage_ref["key"])
        if saved is None or hashlib.sha256(saved.payload).hexdigest() != stage_ref["sha256"]:
            raise ValueError("confirmed external cohort snapshot changed")
        stage = load_stage(plan, saved.payload)
        if stage.result.cohort_index != stage_ref["index"]:
            raise ValueError("confirmed external cohort index changed")
        stages[stage_ref["index"]] = (stage, saved.etag)
    return prepare_saved_stages(plan, stages, confirmed_partial=value["confirmed_partial"])


def available_ready(store: ReceiptStore) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    latest: dict[str, tuple[datetime, dict[str, str]]] = {}
    failed = []
    for key, _ in store.iter_objects("coordination/external-assessment/"):
        if "/ready-" not in key or not key.endswith(".json"):
            continue
        run_id = key.split("/")[-2]
        try:
            plan = read_plan(store, run_id)
            if plan is None:
                raise ValueError("ready selection has no exact plan")
            if read_terminal(store, plan) is not None:
                continue
            saved = read_private(store, key)
            if saved is None:
                raise ValueError("listed external ready selection is missing")
            value = json.loads(saved.payload)
            if value["plan_sha256"] != plan.plan_sha256:
                raise ValueError("ready plan binding changed")
            clock = datetime.fromisoformat(value["created_at"])
            reference = {"key": key, "sha256": hashlib.sha256(saved.payload).hexdigest()}
            if run_id not in latest or clock > latest[run_id][0]:
                latest[run_id] = (clock, reference)
        except LakePublishError as error:
            if str(error).startswith("R2 "):
                raise
            failed.append({"run_id": run_id, "reason": type(error).__name__})
        except (ValueError, KeyError, TypeError) as error:
            failed.append({"run_id": run_id, "reason": type(error).__name__})
    return [value[1] for _, value in sorted(latest.items())], failed
