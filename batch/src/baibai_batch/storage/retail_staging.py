"""Conditional private stage handoff in the existing stores bucket."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

from baibai_batch.jobs.retail_cohorts import (
    CohortJournal,
    RunPlan,
    journal_bytes,
    load_journal,
    load_plan,
    plan_bytes,
)
from baibai_batch.storage.lake_publish import LakePublishError, ObjectStore
from baibai_engine.batch_api import RetailSource

MAX_PRIVATE_OBJECT_BYTES = 2_000_000
CHECKPOINT_FORMAT = "retail-private-checkpoint-v2"
MAX_PAIR_RETRIES = 5
PARTIAL_TERMINAL_FORMAT = "retail-partially-published-batch-v1"
_TERMINAL_KEYS = {
    "format",
    "plan_sha256",
    "release_id",
    "release_manifest_sha256",
    "expected_pairs",
    "observations",
    "readback",
}
_TERMINAL_STATES = {"success", "normal_null", "failure", "unattempted", "uncertain"}


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
            raise ValueError("invalid retail published batch")
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
            value["format"] not in ("retail-published-batch-v1", PARTIAL_TERMINAL_FORMAT)
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
        raise ValueError("invalid retail published batch")
    return value


def save_new_plan(store: ObjectStore, plan: RunPlan) -> None:
    put_private(store, prefix(plan.run_id) + "/plan.json", plan_bytes(plan), previous_etag=None)


@dataclass(frozen=True)
class PairRetryState:
    source: RetailSource
    ticker: str
    retries_used: int
    next_retry_utc: datetime | None = None
    retry_after_unrepresentable: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(cast("object", self.source), str)
            or self.source not in {"monex", "rakuten"}
            or type(self.ticker) is not str
            or re.fullmatch(r"[0-9][0-9A-Z]{3}", self.ticker) is None
            or type(self.retries_used) is not int
            or not 0 <= self.retries_used <= MAX_PAIR_RETRIES
            or type(cast("object", self.retry_after_unrepresentable)) is not bool
            or (self.retry_after_unrepresentable and self.next_retry_utc is not None)
        ):
            raise ValueError("invalid retail pair retry state")
        if self.next_retry_utc is not None and (
            not isinstance(cast("object", self.next_retry_utc), datetime)
            or self.next_retry_utc.tzinfo is None
            or self.next_retry_utc.utcoffset() != UTC.utcoffset(None)
        ):
            raise ValueError("retail retry deadline requires UTC")


@dataclass(frozen=True)
class Checkpoint:
    journal: CohortJournal
    requests_started: int = 0
    confirmed_http_requests: int = 0
    response_content_bytes: int = 0
    last_response_finished_at_utc: datetime | None = None
    retry_states: tuple[PairRetryState, ...] = ()

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
        if type(self.retry_states) is not tuple or any(
            not isinstance(item, PairRetryState) for item in self.retry_states
        ):
            raise ValueError("retry states require an immutable typed tuple")
        pairs = [(item.source, item.ticker) for item in self.retry_states]
        if pairs != sorted(set(pairs)):
            raise ValueError("duplicate or unordered retail retry pair")
        for retry in self.retry_states:
            entry = next(
                (
                    item
                    for item in self.journal.entries
                    if (item.source, item.ticker) == (retry.source, retry.ticker)
                ),
                None,
            )
            if entry is None:
                raise ValueError("retry pair is outside the cohort")
            rate_limited = (entry.state, entry.reason, entry.http_status) == (
                "failure",
                "rate_limited",
                429,
            )
            if (
                (
                    (retry.next_retry_utc is not None or retry.retry_after_unrepresentable)
                    and not rate_limited
                )
                or (
                    rate_limited
                    and retry.retries_used < MAX_PAIR_RETRIES
                    and retry.next_retry_utc is None
                    and not retry.retry_after_unrepresentable
                )
                or (not rate_limited and retry.retries_used == 0)
                or entry.state == "unattempted"
                or (retry.retries_used == MAX_PAIR_RETRIES and retry.next_retry_utc is not None)
            ):
                raise ValueError("retry metadata does not match the confirmed pair state")

    def retry_for(self, source: RetailSource, ticker: str) -> PairRetryState | None:
        return next(
            (item for item in self.retry_states if (item.source, item.ticker) == (source, ticker)),
            None,
        )


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
            "retry_states": [
                {
                    "source": item.source,
                    "ticker": item.ticker,
                    "retries_used": item.retries_used,
                    "next_retry_utc": item.next_retry_utc.isoformat()
                    if item.next_retry_utc
                    else None,
                    "retry_after_unrepresentable": item.retry_after_unrepresentable,
                }
                for item in checkpoint.retry_states
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _checkpoint_object(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate retail checkpoint field")
        result[key] = value
    return result


def load_checkpoint(plan: RunPlan, payload: bytes) -> Checkpoint:
    value = json.loads(payload, object_pairs_hook=_checkpoint_object)
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
            "retry_states",
        }
        or value["format"] != CHECKPOINT_FORMAT
    ):
        raise ValueError("invalid retail checkpoint")
    finished = value["last_response_finished_at_utc"]
    if finished is not None and not isinstance(finished, str):
        raise ValueError("invalid retail response timestamp")
    retries = value["retry_states"]
    if not isinstance(retries, list):
        raise ValueError("invalid retail retry states")
    retry_states = []
    for item in retries:
        if (
            not isinstance(item, dict)
            or set(item)
            != {"source", "ticker", "retries_used", "next_retry_utc", "retry_after_unrepresentable"}
            or (item["next_retry_utc"] is not None and not isinstance(item["next_retry_utc"], str))
        ):
            raise ValueError("invalid retail retry fields")
        retry_states.append(
            PairRetryState(
                item["source"],
                item["ticker"],
                item["retries_used"],
                datetime.fromisoformat(item["next_retry_utc"])
                if item["next_retry_utc"] is not None
                else None,
                item["retry_after_unrepresentable"],
            )
        )
    checkpoint = Checkpoint(
        load_journal(plan, json.dumps(value["journal"]).encode()),
        value["requests_started"],
        value["confirmed_http_requests"],
        value["response_content_bytes"],
        datetime.fromisoformat(finished) if finished is not None else None,
        tuple(retry_states),
    )
    maximum = sum(
        (1 if source == "monex" else 2)
        * (1 + (retry.retries_used if (retry := checkpoint.retry_for(source, ticker)) else 0))
        for source, ticker in plan.cohorts[checkpoint.journal.cohort_index].pairs
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
