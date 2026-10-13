"""Conditional private stage handoff in the existing stores bucket."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import cast

from baibai_batch.jobs.external_assessment_cohorts import (
    CohortResult,
    RunPlan,
    load_plan,
    load_result,
    plan_bytes,
    result_value,
    validate_result,
)
from baibai_batch.storage.external_assessment_identity import (
    COORDINATION_PREFIX,
    PARTIAL_TERMINAL_FORMAT,
    PUBLISHED_FORMAT,
    STAGE_FORMAT,
)
from baibai_batch.storage.lake_publish import LakePublishError, ObjectStore

MAX_PRIVATE_OBJECT_BYTES = 2_000_000
_TERMINAL_KEYS = {
    "format",
    "plan_sha256",
    "release_id",
    "release_manifest_sha256",
    "expected_pairs",
    "observations",
    "readback",
}
_TERMINAL_STATES = {"success", "normal_null", "failure", "unattempted"}


def prefix(run_id: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", run_id) is None:
        raise ValueError("invalid external assessment batch id")
    return f"{COORDINATION_PREFIX}{run_id}"


@dataclass(frozen=True)
class SavedObject:
    payload: bytes
    etag: str


def read_private(store: ObjectStore, key: str) -> SavedObject | None:
    before = store.head(key)
    if before is None:
        return None
    if not before.etag or not 0 < before.size <= MAX_PRIVATE_OBJECT_BYTES:
        raise LakePublishError("invalid private external assessment object")
    payload = store.get_bytes(key)
    after = store.head(key)
    if (
        after is None
        or after.etag != before.etag
        or len(payload) != before.size
        or before.metadata.get("sha256") != hashlib.sha256(payload).hexdigest()
    ):
        raise LakePublishError("private external assessment object changed or failed integrity")
    return SavedObject(payload, before.etag)


def put_private(
    store: ObjectStore, key: str, payload: bytes, *, previous_etag: str | None
) -> SavedObject:
    if not 0 < len(payload) <= MAX_PRIVATE_OBJECT_BYTES:
        raise ValueError("invalid private external assessment payload size")
    digest = hashlib.sha256(payload).hexdigest()
    with tempfile.TemporaryDirectory(prefix="baibai-external-assessment-stage-") as temporary:
        path = Path(temporary) / "payload.json"
        path.write_bytes(payload)
        path.chmod(0o600)
        result = store.put_file(
            key,
            path,
            sha256=digest,
            content_md5=base64.b64encode(
                hashlib.md5(payload, usedforsecurity=False).digest()
            ).decode(),
            content_type="application/json",
            if_match=previous_etag,
            if_none_match=previous_etag is None,
        )
    if not result.etag or result.size != len(payload) or result.metadata.get("sha256") != digest:
        raise LakePublishError("private external assessment write receipt mismatch")
    return SavedObject(payload, result.etag)


def read_plan(store: ObjectStore, run_id: str) -> RunPlan | None:
    saved = read_private(store, prefix(run_id) + "/plan.json")
    if saved is None:
        return None
    plan = load_plan(saved.payload)
    if plan.run_id != run_id:
        raise ValueError("external assessment plan run id mismatch")
    return plan


def terminal_status(terminal: dict[str, object]) -> str:
    return (
        "already_partially_published"
        if terminal["format"] == PARTIAL_TERMINAL_FORMAT
        else "already_published"
    )


def _terminal_object(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError("invalid external assessment published batch")
        result[key] = value
    return result


def _valid_partial_counts(value: dict[str, object], plan: RunPlan) -> bool:
    states, coverage = value["state_counts"], value["receipt_date_coverage"]
    if (
        not isinstance(states, dict)
        or set(states) != _TERMINAL_STATES
        or any(type(count) is not int or count < 0 for count in states.values())
        or sum(states.values()) != value["expected_pairs"]
        or states["success"] + states["normal_null"] != value["observations"]
        or value["observations"] == 0
        or not isinstance(coverage, dict)
    ):
        return False
    total = 0
    by_source = dict.fromkeys(plan.sources, 0)
    for day, counts in coverage.items():
        if not isinstance(day, str):
            return False
        try:
            if date.fromisoformat(day).isoformat() != day:
                return False
        except ValueError:
            return False
        if (
            not isinstance(counts, dict)
            or set(counts) != set(plan.sources)
            or any(type(count) is not int or count < 0 for count in counts.values())
            or sum(counts.values()) == 0
        ):
            return False
        total += sum(counts.values())
        for source in plan.sources:
            by_source[source] += counts[source]
    return total == value["observations"] and all(
        count <= len(plan.tickers) for count in by_source.values()
    )


def read_terminal(store: ObjectStore, plan: RunPlan) -> dict[str, object] | None:
    saved = read_private(store, prefix(plan.run_id) + "/published.json")
    if saved is None:
        return None
    value = json.loads(saved.payload, object_pairs_hook=_terminal_object)
    partial = isinstance(value, dict) and value.get("format") == PARTIAL_TERMINAL_FORMAT
    if (
        not isinstance(value, dict)
        or set(value)
        != (
            _TERMINAL_KEYS | {"state_counts", "receipt_date_coverage"}
            if partial
            else _TERMINAL_KEYS
        )
        or (
            value["format"] not in (PUBLISHED_FORMAT, PARTIAL_TERMINAL_FORMAT)
            or value["plan_sha256"] != plan.plan_sha256
            or type(value["expected_pairs"]) is not int
            or value["expected_pairs"] != len(plan.tickers) * len(plan.sources)
            or type(value["observations"]) is not int
            or not 0 <= value["observations"] <= value["expected_pairs"]
            or value["readback"] != "verified"
            or not isinstance(value["release_id"], str)
            or not value["release_id"]
            or not isinstance(value["release_manifest_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", value["release_manifest_sha256"]) is None
            or (partial and not _valid_partial_counts(value, plan))
        )
    ):
        raise ValueError("invalid external assessment published batch")
    return value


def save_new_plan(store: ObjectStore, plan: RunPlan) -> None:
    put_private(store, prefix(plan.run_id) + "/plan.json", plan_bytes(plan), previous_etag=None)


@dataclass(frozen=True)
class CohortStage:
    result: CohortResult
    http_requests: int = 0
    response_content_bytes: int = 0
    raw_bundle_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(cast("object", self.result), CohortResult) or any(
            type(value) is not int or value < 0
            for value in (self.http_requests, self.response_content_bytes)
        ):
            raise ValueError("invalid external assessment stage accounting")
        if (
            type(self.raw_bundle_keys) is not tuple
            or any(type(key) is not str or not key for key in self.raw_bundle_keys)
            or len(set(self.raw_bundle_keys)) != len(self.raw_bundle_keys)
        ):
            raise ValueError("invalid external assessment raw bundle references")


def stage_bytes(plan: RunPlan, stage: CohortStage) -> bytes:
    validate_result(plan, stage.result)
    return json.dumps(
        {
            "format": STAGE_FORMAT,
            "result": result_value(plan, stage.result),
            "http_requests": stage.http_requests,
            "response_content_bytes": stage.response_content_bytes,
            "raw_bundle_keys": list(stage.raw_bundle_keys),
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _stage_object(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate external assessment stage field")
        result[key] = value
    return result


def load_stage(plan: RunPlan, payload: bytes) -> CohortStage:
    value = json.loads(payload, object_pairs_hook=_stage_object)
    if (
        not isinstance(value, dict)
        or set(value)
        != {"format", "result", "http_requests", "response_content_bytes", "raw_bundle_keys"}
        or value["format"] != STAGE_FORMAT
        or not isinstance(value["raw_bundle_keys"], list)
    ):
        raise ValueError("invalid external assessment cohort stage")
    return CohortStage(
        load_result(plan, value["result"]),
        value["http_requests"],
        value["response_content_bytes"],
        tuple(value["raw_bundle_keys"]),
    )


def stage_key(plan: RunPlan, index: int) -> str:
    if type(index) is not int or not 0 <= index < len(plan.cohorts):
        raise ValueError("invalid external assessment cohort index")
    return prefix(plan.run_id) + f"/cohort-{index:04d}.json"


def read_stage(store: ObjectStore, plan: RunPlan, index: int) -> tuple[CohortStage, str] | None:
    saved = read_private(store, stage_key(plan, index))
    if saved is None:
        return None
    stage = load_stage(plan, saved.payload)
    if stage.result.cohort_index != index:
        raise ValueError("external assessment stage cohort identity mismatch")
    return stage, saved.etag


def read_stages(store: ObjectStore, plan: RunPlan) -> dict[int, tuple[CohortStage, str]]:
    return {
        cohort.index: saved
        for cohort in plan.cohorts
        if (saved := read_stage(store, plan, cohort.index)) is not None
    }


def _require_extension(previous: CohortStage, candidate: CohortStage) -> None:
    if (
        previous.result.plan_sha256 != candidate.result.plan_sha256
        or previous.result.cohort_index != candidate.result.cohort_index
        or len(previous.result.entries) != len(candidate.result.entries)
        or any(
            old != new and old.state != "unattempted"
            for old, new in zip(previous.result.entries, candidate.result.entries, strict=True)
        )
        or candidate.http_requests < previous.http_requests
        or candidate.response_content_bytes < previous.response_content_bytes
        or candidate.raw_bundle_keys[: len(previous.raw_bundle_keys)] != previous.raw_bundle_keys
    ):
        raise ValueError("saved cohort outcomes and receipt times cannot change")


def commit_stage(
    store: ObjectStore,
    plan: RunPlan,
    stage: CohortStage,
    *,
    previous: tuple[CohortStage, str] | None,
) -> CohortStage:
    """Commit once; on an ambiguous PUT adopt the actual saved outcome, never overwrite it."""
    if previous is not None:
        _require_extension(previous[0], stage)
    try:
        put_private(
            store,
            stage_key(plan, stage.result.cohort_index),
            stage_bytes(plan, stage),
            previous_etag=previous[1] if previous else None,
        )
        return stage
    except Exception:
        saved = read_stage(store, plan, stage.result.cohort_index)
        if saved is None:
            raise
        if previous is not None:
            _require_extension(previous[0], saved[0])
        # A competing writer's receipt is authoritative, including its timestamps.
        # The caller reports any still-unrecorded pairs and does not retry a PUT.
        return saved[0]
