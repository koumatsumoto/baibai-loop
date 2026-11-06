"""Bounded private planning state for article slots, never a copy of raw history."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.providers.articles.contracts import SOURCES
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.json import put_private, read_private
from baibai_engine.storage.objects.r2 import LakePublishError, ObjectStore
from baibai_jobs.acquisition.article_discovery import (
    DISCOVERY_FORMAT,
    DISCOVERY_REF_FORMAT,
    LEGACY_DISCOVERY_FORMAT,
    expand_cursor,
)
from baibai_jobs.acquisition.article_frontier import (
    FRONTIER_FORMAT,
    FRONTIER_REF_FORMAT,
    FRONTIER_SOURCES,
    bind_local_stage,
)
from baibai_jobs.acquisition.economic_staging import save_handoff, stage_prefix

ARTICLE_SOURCES = tuple(SOURCES)
TSR_SOURCES = frozenset({"tsr_data", "tsr_flash"})
ARTICLE_NAMESPACE = "article-sources"
ARTICLE_CRON = "13 1,5,9,13,17,21 * * *"
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
    last_stage: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "format": "article-planning-v3",
            "source": self.source,
            "generations": self.generations,
            "last_slot": self.last_slot,
            "not_before": self.not_before,
            "requests": self.requests,
            "cursor": self.cursor,
            "last_stage": self.last_stage,
        }

    def encoded(self) -> bytes:
        source_key(self.source)
        for value in (self.last_slot, self.not_before):
            if value is not None:
                _clock(value)
        if len(self.requests) > MAX_REQUEST_STATES:
            raise ValueError("article request planning bound reached")
        if self.last_stage is not None:
            entry = self.last_stage
            if (
                entry.get("source") != self.source
                or entry.get("namespace") != ARTICLE_NAMESPACE
                or entry.get("market") is not True
            ):
                raise ValueError("article stage differs from its planning source")
            stage_prefix(entry["stage_id"], namespace=ARTICLE_NAMESPACE)
        # Budget actual UTF-8 JSON, not six-byte escapes for each Japanese character.
        # This changes no fields or bounds and never evicts saved cursor/evidence.
        payload = json.dumps(
            self.to_json(),
            sort_keys=True,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > MAX_PLANNING_BYTES:
            raise ValueError("article planning summary exceeds its bytes budget")
        return payload

    @classmethod
    def decode(cls, payload: bytes, source: str) -> ArticlePlanning:
        value = json.loads(payload)
        if value.get("format") not in {
            "article-planning-v1",
            "article-planning-v2",
            "article-planning-v3",
        } or (value.get("source") != source):
            raise ValueError("article planning source/format mismatch")
        if value["format"] == "article-planning-v2" and (
            value["cursor"].get("format") not in {FRONTIER_REF_FORMAT, DISCOVERY_REF_FORMAT}
        ):
            raise ValueError("article planning v2 requires an exact frontier reference")
        result = cls(
            source=source,
            generations=value["generations"],
            last_slot=value["last_slot"],
            not_before=value["not_before"],
            requests=value["requests"],
            cursor=value["cursor"],
            # Old backlog entries remain discoverable by their immutable handoffs.
            # Only the last receipt is needed to bind this acquisition summary.
            last_stage=value["last_stage"]
            if value["format"] == "article-planning-v3"
            else value["pending"][-1]
            if value["pending"]
            else None,
        )
        result.encoded()
        return result


def load_planning(store: ObjectStore, source: str) -> tuple[ArticlePlanning, str]:
    saved = read_private(store, source_key(source))
    if saved is None:
        raise LakePublishError("article planning input missing; publish inputs before acquisition")
    state = ArticlePlanning.decode(saved.payload, source)
    value = json.loads(saved.payload)
    # Do not erase a legacy backlog reference unless its original is discoverable
    # without this mutable summary. This check ends with the first v3 commit.
    if value["format"] != "article-planning-v3":
        for entry in value["pending"]:
            if (
                entry.get("source") != source
                or entry.get("namespace") != ARTICLE_NAMESPACE
                or entry.get("market") is not True
            ):
                raise ValueError("legacy article stage differs from its planning source")
            original = read_private(
                store,
                stage_prefix(entry["stage_id"], namespace=ARTICLE_NAMESPACE) + "/handoff.json",
            )
            if (
                original is None
                or hashlib.sha256(original.payload).hexdigest() != entry["manifest_sha256"]
            ):
                raise LakePublishError("legacy pending article manifest differs or is missing")
            if json.loads(original.payload)["result"]["source"] != source:
                raise ValueError("legacy pending article manifest belongs to another source")
    return state, saved.etag


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


def index_poll_due(source: str, *, now: datetime, received_at: datetime | None) -> bool:
    """A saved native index receipt, not an attempted slot, fulfils a daily poll."""
    if source not in TSR_SOURCES:
        return True
    now = _clock(now.isoformat())
    boundary = now.replace(hour=13, minute=13, second=0, microsecond=0)
    if boundary > now:
        boundary -= timedelta(days=1)
    return received_at is None or _clock(received_at.isoformat()) < boundary


def is_due(
    state: ArticlePlanning, *, slot: datetime, now: datetime, resume_pending: bool = False
) -> bool:
    slot, now = _clock(slot.isoformat()), _clock(now.isoformat())
    if slot != slot_at(slot) or slot > now:
        raise ValueError("invalid article acquisition slot")
    if state.source not in TSR_SOURCES and state.last_slot:
        previous = _clock(state.last_slot)
        if previous > slot or (previous == slot and not resume_pending):
            return False
    return not (state.not_before and _clock(state.not_before) > now)


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
    advance_slot: bool = True,
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
    bound_cursor = cursor
    if cursor.get("format") == FRONTIER_FORMAT:
        bound_cursor = {"format": FRONTIER_REF_FORMAT, "stage": entry}
    elif cursor.get("format") in (DISCOVERY_FORMAT, LEGACY_DISCOVERY_FORMAT):
        bound_cursor = {"format": DISCOVERY_REF_FORMAT, "stage": entry}
    elif cursor.get("pending_evidence"):
        bound_cursor = {
            **cursor,
            "pending_evidence": {
                url: {**link, "stage": entry}
                if link.get("stage", {}).get("stage_id") == stage_id
                else link
                for url, link in cursor["pending_evidence"].items()
            },
        }
    candidate = ArticlePlanning(
        state.source,
        state.generations,
        slot.isoformat() if advance_slot else state.last_slot,
        not_before,
        bound_requests,
        bound_cursor,
        entry,
    )
    put_private(store, source_key(state.source), candidate.encoded(), previous_etag=expected_etag)
    return entry


def _load_workset(
    store: ObjectStore, state: ArticlePlanning, expected_format: str | tuple[str, ...]
) -> tuple[dict[str, Any], dict[str, Any]]:
    entry = state.cursor["stage"]
    if (
        entry.get("source") != state.source
        or entry.get("namespace") != ARTICLE_NAMESPACE
        or entry.get("market") is not True
    ):
        raise ValueError("article frontier source differs")
    saved = read_private(
        store, stage_prefix(entry["stage_id"], namespace=ARTICLE_NAMESPACE) + "/handoff.json"
    )
    if saved is None or hashlib.sha256(saved.payload).hexdigest() != entry["manifest_sha256"]:
        raise LakePublishError("article frontier manifest differs or is missing")
    report = json.loads(saved.payload)["result"]
    frontier = report["planning"]["cursor"]
    formats = (expected_format,) if isinstance(expected_format, str) else expected_format
    if report["source"] != state.source or frontier.get("format") not in formats:
        raise ValueError("article frontier source/format mismatch")
    return frontier, entry


def load_frontier_cursor(store: ObjectStore, state: ArticlePlanning) -> dict[str, Any]:
    """Read one exact workset before HTTP, retaining old cursors for migration."""
    if state.cursor.get("format") != FRONTIER_REF_FORMAT:
        return state.cursor
    frontier, entry = _load_workset(store, state, FRONTIER_FORMAT)
    return {
        **frontier,
        "indexes": [
            {**item, "stage": entry} if item["stage"].get("stage_id") == entry["stage_id"] else item
            for item in frontier["indexes"]
        ],
    }


def load_discovery_cursor(store: ObjectStore, state: ArticlePlanning) -> dict[str, Any]:
    if state.cursor.get("format") != DISCOVERY_REF_FORMAT:
        return expand_cursor(state.cursor)
    cursor, entry = _load_workset(store, state, (DISCOVERY_FORMAT, LEGACY_DISCOVERY_FORMAT))
    cursor = expand_cursor(cursor)

    def bind_reference(value: dict[str, Any]) -> dict[str, Any]:
        value = bind_local_stage(value, entry)
        if value.get("index_evidence") is not None:
            value = {**value, "index_evidence": bind_local_stage(value["index_evidence"], entry)}
        return value

    return {
        **cursor,
        "discoveries": {key: bind_reference(value) for key, value in cursor["discoveries"].items()},
        "discovery_children": {
            url: [{**edge, "parent": bind_reference(edge["parent"])} for edge in edges]
            for url, edges in cursor["discovery_children"].items()
        },
        "pending_evidence": {
            url: bind_local_stage(value, entry)
            for url, value in cursor.get("pending_evidence", {}).items()
        },
    }


def legacy_omission_evidence(
    store: ObjectStore, state: ArticlePlanning
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """Prove a v1 omission is recoverable from its exact complete index window.

    Only the matching committed plan with a full successful body budget qualifies.
    No early failure can have displaced an older pending URL from its retained tail.
    Re-enumerating every retained index plus the saved pending list is a superset
    of this slot's omitted URLs. This does not prove coverage of earlier feed gaps.
    """
    omitted = state.cursor.get("omitted_url_count", 0)
    if state.source not in FRONTIER_SOURCES or not omitted or not state.last_slot:
        return None
    policy = SOURCES[state.source]
    prefix = f"{state.source}-{_clock(state.last_slot):%Y%m%dT%H%M}-"
    candidates = [state.last_stage] if state.last_stage is not None else []
    candidates.extend(value["stage"] for value in state.requests.values() if "stage" in value)
    seen = set()
    for entry in candidates:
        identity = (entry.get("stage_id"), entry.get("manifest_sha256"))
        if identity in seen or not str(identity[0]).startswith(prefix):
            continue
        seen.add(identity)
        if (
            entry.get("source") != state.source
            or entry.get("namespace") != ARTICLE_NAMESPACE
            or entry.get("market") is not True
        ):
            continue
        saved = read_private(
            store, stage_prefix(entry["stage_id"], namespace=ARTICLE_NAMESPACE) + "/handoff.json"
        )
        if saved is None or hashlib.sha256(saved.payload).hexdigest() != entry["manifest_sha256"]:
            raise LakePublishError("legacy frontier manifest differs or is missing")
        report = json.loads(saved.payload)["result"]
        prior = report.get("planning", {})
        bound_requests = {
            url: bind_local_stage(value, entry) for url, value in prior.get("requests", {}).items()
        }
        old_cursor = prior.get("cursor", {})
        bound_cursor = {
            **old_cursor,
            "pending_evidence": {
                url: bind_local_stage(value, entry)
                for url, value in old_cursor.get("pending_evidence", {}).items()
            },
        }
        if (
            report.get("source") != state.source
            or report.get("status") != "partial"
            or report.get("continuation_only") is not False
            or report.get("failures") != ["pending_cursor_overflow"]
            or report.get("bodies") != policy.max_bodies
            or report.get("pages") != policy.max_pages
            or report.get("omitted_url_count") != omitted
            or prior.get("slot") != state.last_slot
            or prior.get("not_before") != state.not_before
            or bound_requests != state.requests
            or bound_cursor != state.cursor
        ):
            continue
        references = [
            {"stage": entry, "index_receipt_id": receipt_id}
            for receipt_id in report["receipt_ids"]
            if report["projection_kinds"].get(receipt_id) == "index"
        ]
        if len(references) >= policy.max_pages:
            return entry, references
    return None
