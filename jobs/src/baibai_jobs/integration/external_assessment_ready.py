"""Freeze exact inspected cohort stages for the independently scheduled integration."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from baibai_engine.storage.objects.r2 import (
    LakeCASConflict,
    LakePublishError,
    ObjectStore,
    R2Error,
)
from baibai_jobs.acquisition.external_assessments.cohorts import RunPlan, summarize
from baibai_jobs.acquisition.external_assessments.raw import preserve_raw_bundle
from baibai_jobs.acquisition.external_assessments.staging import (
    CohortStage,
    load_stage,
    prefix,
    put_private,
    read_plan,
    read_private,
    read_stages,
    read_terminal,
    require_extension,
    stage_bytes,
)
from baibai_jobs.acquisition.receipts import ReceiptStore


@dataclass(frozen=True)
class PreparedPlan:
    plan: RunPlan
    stages: dict[int, tuple[CohortStage, str]]
    accounting: dict[str, object]
    confirmed_partial: bool
    cumulative: bool = False


def prepare_plan(
    store: ObjectStore, plan: RunPlan, *, confirmed_partial: bool = False, cumulative: bool = False
) -> PreparedPlan:
    """Validate one exact plan's handoff without importing or publishing anything."""
    return prepare_saved_stages(
        plan, read_stages(store, plan), confirmed_partial=confirmed_partial, cumulative=cumulative
    )


def prepare_saved_stages(
    plan: RunPlan,
    stages: dict[int, tuple[CohortStage, str]],
    *,
    confirmed_partial: bool = False,
    cumulative: bool = False,
) -> PreparedPlan:
    """Apply the same exact/partial checks to an immutable stage selection."""
    if cumulative and confirmed_partial:
        raise ValueError("cumulative publication cannot close an explicit partial plan")
    results = [stage.result for stage, _ in stages.values()]
    if not (confirmed_partial or cumulative) and any(
        entry.http_status in {403, 429} for result in results for entry in result.entries
    ):
        raise ValueError(
            "blocked provider response requires explicit confirmed partial publication"
        )
    accounting = summarize(plan, results, require_all=not (confirmed_partial or cumulative))
    if not (confirmed_partial or cumulative) and not accounting["attempted_completeness"]:
        raise ValueError("external assessment publication requires all fixed pairs attempted")
    staged_observations = tuple(item for result in results for item in result.observations)
    if confirmed_partial and not staged_observations:
        raise ValueError(
            "external assessment partial publication requires confirmed typed observations"
        )
    if cumulative and not any(
        entry.state != "unattempted" for result in results for entry in result.entries
    ):
        raise ValueError("cumulative handoff requires at least one attempted pair")
    return PreparedPlan(plan, stages, accounting, confirmed_partial, cumulative)


def save_ready(store: ReceiptStore, item: PreparedPlan) -> dict[str, str]:
    plan = item.plan
    stages = []
    acknowledged = acknowledged_bundles(store, plan) if item.cumulative else {}
    for index, (stage, _) in sorted(item.stages.items()):
        if item.cumulative:
            for key in stage.raw_bundle_keys:
                if key not in acknowledged:
                    preserve_raw_bundle(store, key)
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
    if item.cumulative:
        value["mode"] = "cumulative-v1"
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
    mode = value.get("mode")
    if mode not in (None, "cumulative-v1"):
        raise ValueError("unknown external ready mode")
    return prepare_saved_stages(
        plan, stages, confirmed_partial=value["confirmed_partial"], cumulative=mode is not None
    )


def acknowledgement_key(plan: RunPlan, reference: dict[str, str]) -> str:
    return prefix(plan.run_id) + f"/ack-{reference['sha256']}.json"


def read_acknowledgement(
    store: ObjectStore, item: PreparedPlan, reference: dict[str, str]
) -> dict[str, object] | None:
    saved = read_private(store, acknowledgement_key(item.plan, reference))
    if saved is None:
        return None
    value = json.loads(saved.payload)
    if (
        not isinstance(value, dict)
        or value.get("format") != "external-assessment-integration-ack-v1"
        or value.get("reference") != reference
        or value.get("plan_sha256") != item.plan.plan_sha256
        or value.get("readback") != "verified"
        or not isinstance(value.get("release_id"), str)
        or not value["release_id"]
        or not isinstance(value.get("release_manifest_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", value["release_manifest_sha256"]) is None
        or value.get("state_counts") != item.accounting["state_counts"]
        or value.get("expected_pairs") != item.accounting["expected_pairs"]
        or value.get("observations")
        != sum(len(stage.result.observations) for stage, _ in item.stages.values())
    ):
        raise ValueError("external assessment acknowledgement binding changed")
    return cast("dict[str, object]", value)


def _contains(new: PreparedPlan, old: PreparedPlan) -> bool:
    if new.plan != old.plan or not old.stages.keys() <= new.stages.keys():
        return False
    try:
        for index, (stage, _) in old.stages.items():
            require_extension(stage, new.stages[index][0])
    except ValueError:
        return False
    return True


def available_ready(store: ReceiptStore) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    candidates: dict[str, list[tuple[datetime, dict[str, str], PreparedPlan]]] = {}
    failed = []
    invalid: set[str] = set()
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
            clock = datetime.fromisoformat(value["created_at"])
            if clock.tzinfo is None:
                raise ValueError("external ready clock must have a timezone")
            reference = {"key": key, "sha256": hashlib.sha256(saved.payload).hexdigest()}
            item = load_ready(store, reference)
            candidates.setdefault(run_id, []).append((clock, reference, item))
        except (R2Error, LakeCASConflict):
            raise
        except (LakePublishError, ValueError, KeyError, TypeError) as error:
            invalid.add(run_id)
            failed.append({"run_id": run_id, "reason": type(error).__name__})
    ready = []
    for run_id, entries in sorted(candidates.items()):
        if run_id in invalid:
            continue
        try:
            # 旧明示partialはplanを閉じる本人の操作。新しい累積ackへ読み替えない。
            legacy_partial = [entry for entry in entries if entry[2].confirmed_partial]
            if legacy_partial:
                selected = max(legacy_partial, key=lambda entry: entry[0])
            elif any(entry[2].cumulative for entry in entries):
                supersets = [
                    entry
                    for entry in entries
                    if entry[2].cumulative and all(_contains(entry[2], old[2]) for old in entries)
                ]
                if not supersets:
                    raise ValueError("external cumulative ready sets are incomparable")
                # 集合包含を先に確認。後着の小さい集合やclock skewで原本を落とさない。
                selected = max(supersets, key=lambda entry: entry[0])
            else:
                selected = max(entries, key=lambda entry: entry[0])
            _, reference, item = selected
            if not item.cumulative or read_acknowledgement(store, item, reference) is None:
                ready.append(reference)
        except (R2Error, LakeCASConflict):
            raise
        except (LakePublishError, ValueError, KeyError, TypeError) as error:
            failed.append({"run_id": run_id, "reason": type(error).__name__})
    return ready, failed


def acknowledged_bundles(store: ReceiptStore, plan: RunPlan) -> dict[str, dict[str, str]]:
    """既にcanonical L1へ読戻した原本を、短期bundleの期限と切り離して参照する。"""
    bundles: dict[str, dict[str, str]] = {}
    for key, _ in store.iter_objects(prefix(plan.run_id) + "/ack-"):
        saved = read_private(store, key)
        if saved is None:
            raise ValueError("external acknowledgement disappeared")
        value = json.loads(saved.payload)
        reference = value["reference"]
        item = load_ready(store, reference)
        if item.plan != plan or key != acknowledgement_key(plan, reference):
            raise ValueError("external acknowledged plan changed")
        ack = read_acknowledgement(store, item, reference)
        assert ack is not None
        allowed = {bundle for stage, _ in item.stages.values() for bundle in stage.raw_bundle_keys}
        receipts = ack.get("bundle_receipts", {})
        if not isinstance(receipts, dict) or not receipts.keys() <= allowed:
            raise ValueError("external acknowledgement raw scope changed")
        for bundle, identities in receipts.items():
            if not isinstance(identities, dict) or not identities:
                raise ValueError("external acknowledgement requires exact raw receipt identities")
            if bundle in bundles and bundles[bundle] != identities:
                raise ValueError("acknowledged raw receipt identities conflict")
            bundles[bundle] = identities
    return bundles
