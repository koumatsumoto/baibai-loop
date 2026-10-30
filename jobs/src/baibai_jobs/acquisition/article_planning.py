"""Bounded private planning state for article slots, never a copy of raw history."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.providers.articles.contracts import SOURCES
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import LakePublishError, ObjectStore
from baibai_jobs.acquisition.economic_staging import save_handoff, stage_prefix
from baibai_jobs.acquisition.external_assessments.staging import put_private, read_private

ARTICLE_SOURCES = tuple(SOURCES)
TSR_SOURCES = frozenset({"tsr_data", "tsr_flash"})
ARTICLE_NAMESPACE = "article-sources"
ARTICLE_CRON = "13 1,5,9,13,17,21 * * *"
MAX_PENDING_STAGES = 42
MAX_PLANNING_BYTES = 128 * 1024
MAX_REQUEST_STATES = 128


def source_key(source: str) -> str:
    if source not in ARTICLE_SOURCES:
        raise ValueError("unsupported article source")
    return f"coordination/article-planning/{source}.json"


def _clock(value: str) -> datetime:
    clock = datetime.fromisoformat(value)
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError("article clock must include timezone")
    return clock.astimezone(UTC)


def slot_at(created_at: datetime) -> datetime:
    """The most recent four-hour slot; actual receipt time remains independent."""
    instant = _clock(created_at.isoformat())
    boundary = instant.replace(hour=1, minute=13, second=0, microsecond=0)
    return boundary + timedelta(hours=4 * ((instant - boundary) // timedelta(hours=4)))


@dataclass(frozen=True)
class ArticlePlanning:
    source: str
    generations: dict[str, str]
    last_slot: str | None = None
    not_before: str | None = None
    # Adapter-owned URL/validator/cursor/body references, with a strict bytes cap.
    requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    cursor: dict[str, Any] = field(default_factory=dict)
    pending: tuple[dict[str, Any], ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "format": "article-planning-v1",
            "source": self.source,
            "generations": self.generations,
            "last_slot": self.last_slot,
            "not_before": self.not_before,
            "requests": self.requests,
            "cursor": self.cursor,
            "pending": list(self.pending),
        }

    def encoded(self) -> bytes:
        source_key(self.source)
        for value in (self.last_slot, self.not_before):
            if value is not None:
                _clock(value)
        if len(self.requests) > MAX_REQUEST_STATES or len(self.pending) > MAX_PENDING_STAGES:
            raise ValueError("article planning bound reached; drain or recover exact stages")
        for entry in self.pending:
            if (
                entry.get("source") != self.source
                or entry.get("namespace") != ARTICLE_NAMESPACE
                or entry.get("market") is not True
            ):
                raise ValueError("article pending entry differs from its planning source")
            stage_prefix(entry["stage_id"], namespace=ARTICLE_NAMESPACE)
        payload = json.dumps(self.to_json(), sort_keys=True, allow_nan=False).encode()
        if len(payload) > MAX_PLANNING_BYTES:
            raise ValueError("article planning summary exceeds its bytes budget")
        return payload

    @classmethod
    def decode(cls, payload: bytes, source: str) -> ArticlePlanning:
        value = json.loads(payload)
        if value.get("format") != "article-planning-v1" or value.get("source") != source:
            raise ValueError("article planning source/format mismatch")
        result = cls(
            source=source,
            generations=value["generations"],
            last_slot=value["last_slot"],
            not_before=value["not_before"],
            requests=value["requests"],
            cursor=value["cursor"],
            pending=tuple(value["pending"]),
        )
        result.encoded()
        return result


def load_planning(store: ObjectStore, source: str) -> tuple[ArticlePlanning, str]:
    saved = read_private(store, source_key(source))
    if saved is None:
        raise LakePublishError("article planning input missing; publish inputs before acquisition")
    return ArticlePlanning.decode(saved.payload, source), saved.etag


def publish_planning_input(store: ObjectStore, source: str, generations: dict[str, str]) -> None:
    """Provision a missing summary without racing an in-flight acquisition CAS."""
    key = source_key(source)
    prior = read_private(store, key)
    if prior is not None:
        # The summary's generation label is carried, never checked, by acquisition.
        # Daily input refresh must not invalidate the ETag it loaded before a host queue.
        ArticlePlanning.decode(prior.payload, source)
        return
    put_private(store, key, ArticlePlanning(source, generations).encoded(), previous_etag=None)


def is_due(state: ArticlePlanning, *, slot: datetime, now: datetime) -> bool:
    slot, now = _clock(slot.isoformat()), _clock(now.isoformat())
    if slot != slot_at(slot) or slot > now:
        raise ValueError("invalid article acquisition slot")
    if state.last_slot and _clock(state.last_slot) >= slot:
        return False
    if state.not_before and _clock(state.not_before) > now:
        return False
    if len(state.pending) >= MAX_PENDING_STAGES:
        raise ValueError("article pending stage limit reached; integration/recovery required")
    # TSR runs in the pre-integration slot once per UTC day. A delayed run records
    # its actual received_at and never claims to have acquired at the slot time.
    return state.source not in TSR_SOURCES or slot.hour == 13


def commit_stage(
    store: ObjectStore,
    *,
    state: ArticlePlanning,
    expected_etag: str,
    slot: datetime,
    stage_id: str,
    directory: Path,
    requests: dict[str, dict[str, Any]],
    cursor: dict[str, Any],
    not_before: str | None,
) -> dict[str, Any]:
    """Commit raw first; a CAS failure leaves an exact recoverable immutable stage."""
    report = json.loads((directory / "result.json").read_text())
    if report["source"] != state.source:
        raise ValueError("article handoff belongs to another source")
    _clock(report["started_at"])
    # Planning is also immutable in this handoff, so a pointer failure does not
    # require guessing the cursor or performing another provider GET.
    report["planning"] = {
        "slot": slot.isoformat(),
        "requests": requests,
        "cursor": cursor,
        "not_before": not_before,
    }
    write_json_atomic(directory / "result.json", report)
    # Never discard successfully received L1 when planning exceeds its bound.
    # The immutable stage remains available for exact recovery even if summary validation fails.
    placeholder = {
        "market": True,
        "namespace": ARTICLE_NAMESPACE,
        "source": state.source,
        "stage_id": stage_id,
        "manifest_sha256": "0" * 64,
        "started_at": report["started_at"],
        "asof": report["asof"],
    }
    save_handoff(store, stage_id, directory, namespace=ARTICLE_NAMESPACE)
    saved = read_private(
        store, stage_prefix(stage_id, namespace=ARTICLE_NAMESPACE) + "/handoff.json"
    )
    if saved is None:
        raise LakePublishError("saved article handoff is missing")
    entry = {**placeholder, "manifest_sha256": hashlib.sha256(saved.payload).hexdigest()}
    write_json_atomic(directory / "stage.json", entry)
    # New 200 baselines refer to this just-saved stage. Its manifest cannot
    # contain its own digest; only the mutable summary fills this exact closure.
    bound_requests = {
        url: {**value, "stage": entry}
        if value.get("stage", {}).get("stage_id") == stage_id
        else value
        for url, value in requests.items()
    }
    candidate = ArticlePlanning(
        state.source,
        state.generations,
        slot.isoformat(),
        not_before,
        bound_requests,
        cursor,
        (*state.pending, entry),
    )
    put_private(store, source_key(state.source), candidate.encoded(), previous_etag=expected_etag)
    return entry


def pending_handoffs(store: ObjectStore) -> list[dict[str, Any]]:
    """One bounded summary per source; no LIST or history/body download."""
    result: list[dict[str, Any]] = []
    for source in ARTICLE_SOURCES:
        saved = read_private(store, source_key(source))
        if saved is not None:
            try:
                result.extend(ArticlePlanning.decode(saved.payload, source).pending)
            except (ValueError, KeyError, TypeError, AttributeError):
                result.append(
                    {
                        "source": source,
                        "market": True,
                        "namespace": ARTICLE_NAMESPACE,
                        "stage_id": f"{source}-planning",
                        "manifest_sha256": "",
                        "invalid": "invalid_planning_state",
                    }
                )
    return sorted(
        result, key=lambda entry: (entry["source"], entry.get("started_at", ""), entry["stage_id"])
    )


def acknowledge_stage(store: ObjectStore, entry: dict[str, Any]) -> None:
    """After readback/seal, remove only that entry; never rewind live validators."""
    state, etag = load_planning(store, entry["source"])
    remaining = tuple(
        item
        for item in state.pending
        if (item["stage_id"], item["manifest_sha256"])
        != (entry["stage_id"], entry["manifest_sha256"])
    )
    if remaining != state.pending:
        put_private(
            store,
            source_key(state.source),
            replace(state, pending=remaining).encoded(),
            previous_etag=etag,
        )
