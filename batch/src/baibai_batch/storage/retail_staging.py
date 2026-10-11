"""Conditional private stage handoff in the existing stores bucket."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from baibai_batch.jobs.retail_cohorts import (
    CohortJournal,
    RunPlan,
    journal_bytes,
    load_journal,
    load_plan,
    plan_bytes,
)
from baibai_batch.storage.lake_publish import LakePublishError, ObjectStore

MAX_PRIVATE_OBJECT_BYTES = 2_000_000
CHECKPOINT_FORMAT = "retail-private-checkpoint-v1"


def prefix(run_id: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", run_id) is None:
        raise ValueError("invalid retail batch id")
    return f"coordination/retail-dynamics/{run_id}"


@dataclass(frozen=True)
class SavedObject:
    payload: bytes
    etag: str


def read_private(store: ObjectStore, key: str) -> SavedObject | None:
    before = store.head(key)
    if before is None:
        return None
    if not before.etag or not 0 < before.size <= MAX_PRIVATE_OBJECT_BYTES:
        raise LakePublishError("invalid private retail object")
    payload = store.get_bytes(key)
    after = store.head(key)
    if (
        after is None
        or after.etag != before.etag
        or len(payload) != before.size
        or before.metadata.get("sha256") != hashlib.sha256(payload).hexdigest()
    ):
        raise LakePublishError("private retail object changed or failed integrity")
    return SavedObject(payload, before.etag)


def put_private(
    store: ObjectStore, key: str, payload: bytes, *, previous_etag: str | None
) -> SavedObject:
    if not 0 < len(payload) <= MAX_PRIVATE_OBJECT_BYTES:
        raise ValueError("invalid private retail payload size")
    digest = hashlib.sha256(payload).hexdigest()
    with tempfile.TemporaryDirectory(prefix="baibai-retail-stage-") as temporary:
        path = Path(temporary) / "payload.json"
        path.write_bytes(payload)
        path.chmod(0o600)
        # A conflict or ambiguous response always stops before the next HTTP.
        # Do not infer ownership from another writer's identical payload.
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
        raise LakePublishError("private retail write receipt mismatch")
    return SavedObject(payload, result.etag)


def read_plan(store: ObjectStore, run_id: str) -> RunPlan | None:
    saved = read_private(store, prefix(run_id) + "/plan.json")
    if saved is None:
        return None
    plan = load_plan(saved.payload)
    if plan.run_id != run_id:
        raise ValueError("retail plan run id mismatch")
    return plan


def read_terminal(store: ObjectStore, plan: RunPlan) -> dict[str, object] | None:
    saved = read_private(store, prefix(plan.run_id) + "/published.json")
    if saved is None:
        return None
    value = json.loads(saved.payload)
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "format",
            "plan_sha256",
            "release_id",
            "release_manifest_sha256",
            "expected_pairs",
            "observations",
            "readback",
        }
        or (
            value["format"] != "retail-published-batch-v1"
            or value["plan_sha256"] != plan.plan_sha256
            or value["expected_pairs"] != len(plan.tickers) * len(plan.sources)
            or type(value["observations"]) is not int
            or not 0 <= value["observations"] <= value["expected_pairs"]
            or value["readback"] != "verified"
            or not isinstance(value["release_id"], str)
            or not value["release_id"]
            or not isinstance(value["release_manifest_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", value["release_manifest_sha256"]) is None
        )
    ):
        raise ValueError("invalid retail published batch")
    return value


def save_new_plan(store: ObjectStore, plan: RunPlan) -> None:
    put_private(store, prefix(plan.run_id) + "/plan.json", plan_bytes(plan), previous_etag=None)


@dataclass(frozen=True)
class Checkpoint:
    journal: CohortJournal
    requests_started: int = 0
    confirmed_http_requests: int = 0
    response_content_bytes: int = 0
    last_response_finished_at_utc: datetime | None = None

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 0
            for value in (
                self.requests_started,
                self.confirmed_http_requests,
                self.response_content_bytes,
            )
        ):
            raise ValueError("invalid retail request accounting")
        if self.confirmed_http_requests > self.requests_started:
            raise ValueError("retail confirmed requests exceed reservations")
        if self.last_response_finished_at_utc is not None and (
            self.last_response_finished_at_utc.tzinfo is None
            or self.last_response_finished_at_utc.utcoffset() != UTC.utcoffset(None)
        ):
            raise ValueError("retail receipt metadata requires UTC")


def checkpoint_bytes(plan: RunPlan, checkpoint: Checkpoint) -> bytes:
    return json.dumps(
        {
            "format": CHECKPOINT_FORMAT,
            "journal": json.loads(journal_bytes(plan, checkpoint.journal)),
            "requests_started": checkpoint.requests_started,
            "confirmed_http_requests": checkpoint.confirmed_http_requests,
            "response_content_bytes": checkpoint.response_content_bytes,
            "last_response_finished_at_utc": (
                checkpoint.last_response_finished_at_utc.isoformat()
                if checkpoint.last_response_finished_at_utc is not None
                else None
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def load_checkpoint(plan: RunPlan, payload: bytes) -> Checkpoint:
    value = json.loads(payload)
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "format",
            "journal",
            "requests_started",
            "confirmed_http_requests",
            "response_content_bytes",
            "last_response_finished_at_utc",
        }
        or value["format"] != CHECKPOINT_FORMAT
    ):
        raise ValueError("invalid retail checkpoint")
    finished = value["last_response_finished_at_utc"]
    if finished is not None and not isinstance(finished, str):
        raise ValueError("invalid retail response timestamp")
    checkpoint = Checkpoint(
        load_journal(plan, json.dumps(value["journal"]).encode()),
        value["requests_started"],
        value["confirmed_http_requests"],
        value["response_content_bytes"],
        datetime.fromisoformat(finished) if finished is not None else None,
    )
    maximum = sum(
        1 if source == "monex" else 2
        for source, _ in plan.cohorts[checkpoint.journal.cohort_index].pairs
    )
    if checkpoint.requests_started > maximum:
        raise ValueError("retail request count exceeds fixed cohort budget")
    return checkpoint


def checkpoint_key(plan: RunPlan, index: int) -> str:
    if type(index) is not int or not 0 <= index < len(plan.cohorts):
        raise ValueError("invalid retail cohort index")
    return prefix(plan.run_id) + f"/cohort-{index:04d}.json"


def read_checkpoints(store: ObjectStore, plan: RunPlan) -> dict[int, tuple[Checkpoint, str]]:
    result = {}
    for cohort in plan.cohorts:
        saved = read_private(store, checkpoint_key(plan, cohort.index))
        if saved is not None:
            checkpoint = load_checkpoint(plan, saved.payload)
            if checkpoint.journal.cohort_index != cohort.index:
                raise ValueError("retail checkpoint cohort identity mismatch")
            result[cohort.index] = checkpoint, saved.etag
    return result
