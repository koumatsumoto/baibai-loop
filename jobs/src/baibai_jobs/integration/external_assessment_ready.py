"""Freeze exact inspected cohort stages for the independently scheduled integration."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from baibai_engine.storage.objects.json import (
    put_private,
    read_fixed_private,
    read_private,
    read_write_once_private,
)
from baibai_engine.storage.objects.r2 import (
    LakeCASConflict,
    LakePublishError,
    ObjectStore,
    R2Error,
)
from baibai_jobs.acquisition.external_assessments.cohorts import RunPlan, summarize
from baibai_jobs.acquisition.external_assessments.identity import COORDINATION_PREFIX
from baibai_jobs.acquisition.external_assessments.raw import preserve_raw_bundle
from baibai_jobs.acquisition.external_assessments.staging import (
    CohortStage,
    load_stage,
    prefix,
    read_plan,
    read_terminal,
    require_extension,
    stage_bytes,
    valid_state_counts,
)
from baibai_jobs.acquisition.receipts import ReceiptStore


@dataclass(frozen=True)
class PreparedPlan:
    plan: RunPlan
    stages: dict[int, tuple[CohortStage, str]]
    accounting: dict[str, object]
    confirmed_partial: bool
    cumulative: bool = False


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
        entry.http_status in {401, 403, 429} for result in results for entry in result.entries
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
    saved = read_fixed_private(store, reference["key"], sha256=reference["sha256"])
    if saved is None:
        raise ValueError("fixed external ready selection changed")
    value = json.loads(saved.payload)
    plan = _bound_plan(read_plan(store, value["run_id"]), value)
    return _prepare_ready(store, value, plan, {})


def _bound_plan(plan: RunPlan | None, value: dict[str, Any]) -> RunPlan:
    if (
        plan is None
        or plan.run_id != value["run_id"]
        or plan.plan_sha256 != value["plan_sha256"]
        or plan.sources != (value["source"],)
    ):
        raise ValueError("external ready selection differs from its exact plan")
    return plan


def _prepare_ready(
    store: ObjectStore,
    value: dict[str, Any],
    plan: RunPlan,
    confirmed_stages: dict[str, tuple[CohortStage, str]],
) -> PreparedPlan:
    stages = {}
    if type(value["confirmed_partial"]) is not bool:
        raise ValueError("external partial confirmation must be explicit")
    for stage_ref in value["stages"]:
        expected_key = prefix(plan.run_id) + f"/confirmed-stages/{stage_ref['sha256']}.json"
        if stage_ref["key"] != expected_key or stage_ref["index"] in stages:
            raise ValueError("invalid confirmed stage scope")
        confirmed = confirmed_stages.get(expected_key)
        if confirmed is None:
            saved = read_fixed_private(store, expected_key, sha256=stage_ref["sha256"])
            if saved is None:
                raise ValueError("confirmed external cohort snapshot changed")
            confirmed = (load_stage(plan, saved.payload), saved.etag)
            confirmed_stages[expected_key] = confirmed
        stage, _ = confirmed
        if stage.result.cohort_index != stage_ref["index"]:
            raise ValueError("confirmed external cohort index changed")
        stages[stage_ref["index"]] = confirmed
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
    return _validate_acknowledgement(item.plan, reference, json.loads(saved.payload), item)


def _validate_acknowledgement(
    plan: RunPlan, reference: dict[str, str], value: Any, prepared: PreparedPlan | None = None
) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or value.get("format") != "external-assessment-integration-ack-v1"
        or value.get("reference") != reference
        or value.get("plan_sha256") != plan.plan_sha256
        or value.get("readback") != "verified"
        or not isinstance(value.get("release_id"), str)
        or not value["release_id"]
        or not isinstance(value.get("release_manifest_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", value["release_manifest_sha256"]) is None
        or value.get("expected_pairs") != len(plan.tickers) * len(plan.sources)
        or not valid_state_counts(value)
        or value["state_counts"]["unattempted"] == value["expected_pairs"]
        or (prepared is not None and value["state_counts"] != prepared.accounting["state_counts"])
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


type _ReadyEntry = tuple[datetime, dict[str, str], PreparedPlan]


def _select_cumulative(entries: list[_ReadyEntry]) -> _ReadyEntry:
    selected = next(entry for entry in entries if entry[2].cumulative)
    # Extension is transitive. Any superset of all entries must replace this
    # candidate (or tie it); reject incomparable histories in the final pass.
    for entry in entries:
        if entry is selected or not entry[2].cumulative:
            continue
        if _contains(entry[2], selected[2]) and (
            entry[0] > selected[0] or not _contains(selected[2], entry[2])
        ):
            selected = entry
    if not all(_contains(selected[2], old[2]) for old in entries):
        raise ValueError("external cumulative ready sets are incomparable")
    return selected


def _ready_marker(
    store: ObjectStore, plan: RunPlan, key: str
) -> tuple[datetime, dict[str, str], dict[str, Any]]:
    saved = read_write_once_private(store, key)
    if saved is None:
        raise ValueError("listed external ready selection is missing")
    value = json.loads(saved.payload)
    _bound_plan(plan, value)
    clock = datetime.fromisoformat(value["created_at"])
    if clock.tzinfo is None:
        raise ValueError("external ready clock must have a timezone")
    return clock, {"key": key, "sha256": hashlib.sha256(saved.payload).hexdigest()}, value


def _pending_ready(
    store: ReceiptStore,
    run_id: str,
    keys: list[str],
    acknowledgements: set[str],
    *,
    terminal_listed: bool,
) -> dict[str, str] | None:
    plan = read_plan(store, run_id)
    if plan is None:
        raise ValueError("ready selection has no exact plan")
    # 一覧後に新しい終了印が来ても、正規受領済みの同一集合は採用対象に戻らない。
    defer_terminal = not terminal_listed and len(keys) == len(acknowledgements) == 1
    if not defer_terminal and read_terminal(store, plan) is not None:
        return None
    entries: list[_ReadyEntry] = []
    confirmed_stages: dict[str, tuple[CohortStage, str]] = {}
    for key in keys:
        try:
            clock, reference, value = _ready_marker(store, plan, key)
            if (
                len(keys) == 1
                and value.get("mode") == "cumulative-v1"
                and value["confirmed_partial"] is False
            ):
                ack_key = acknowledgement_key(plan, reference)
                if ack_key in acknowledgements:
                    ack = read_write_once_private(store, ack_key)
                    if ack is None:
                        raise ValueError("listed external acknowledgement disappeared")
                    _validate_acknowledgement(plan, reference, json.loads(ack.payload))
                    return None
        except (R2Error, LakeCASConflict):
            raise
        except (LakePublishError, ValueError, KeyError, TypeError):
            if defer_terminal and read_terminal(store, plan) is not None:
                return None
            raise
        if defer_terminal and read_terminal(store, plan) is not None:
            return None
        entries.append((clock, reference, _prepare_ready(store, value, plan, confirmed_stages)))

    legacy_partial = [entry for entry in entries if entry[2].confirmed_partial]
    if legacy_partial:
        selected = max(legacy_partial, key=lambda entry: entry[0])
    elif any(entry[2].cumulative for entry in entries):
        selected = _select_cumulative(entries)
    else:
        selected = max(entries, key=lambda entry: entry[0])
    _, reference, item = selected
    if read_terminal(store, plan) is not None:
        return None
    if item.cumulative and read_acknowledgement(store, item, reference) is not None:
        return None
    return reference


def available_ready(store: ReceiptStore) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    by_run: dict[str, list[str]] = {}
    acknowledgements: dict[str, set[str]] = {}
    terminals: set[str] = set()
    for key, _ in store.iter_objects(COORDINATION_PREFIX):
        run_id, _, name = key.removeprefix(COORDINATION_PREFIX).partition("/")
        if name.endswith(".json"):
            if name.startswith("ready-"):
                by_run.setdefault(run_id, []).append(key)
            elif name.startswith("ack-"):
                acknowledgements.setdefault(run_id, set()).add(key)
            elif name == "published.json":
                terminals.add(run_id)
    ready, failed = [], []
    for run_id, keys in sorted(by_run.items()):
        try:
            reference = _pending_ready(
                store,
                run_id,
                keys,
                acknowledgements.get(run_id, set()),
                terminal_listed=run_id in terminals,
            )
            if reference is not None:
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
        ack = _validate_acknowledgement(plan, reference, value, item)
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
