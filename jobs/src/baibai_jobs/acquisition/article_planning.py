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
from baibai_jobs.acquisition.article_frontier import (
    FRONTIER_FORMAT,
    FRONTIER_REF_FORMAT,
    FRONTIER_SOURCES,
    bind_local_stage,
)
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
            "format": "article-planning-v2"
            if self.cursor.get("format") == FRONTIER_REF_FORMAT
            else "article-planning-v1",
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
        if value.get("format") not in {"article-planning-v1", "article-planning-v2"} or (
            value.get("source") != source
        ):
            raise ValueError("article planning source/format mismatch")
        if value["format"] == "article-planning-v2" and (
            value["cursor"].get("format") != FRONTIER_REF_FORMAT
        ):
            raise ValueError("article planning v2 requires an exact frontier reference")
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


def is_due(state: ArticlePlanning, *, slot: datetime, now: datetime, manual: bool = False) -> bool:
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
    return manual or state.source not in TSR_SOURCES or slot.hour == 13


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
    bound_cursor = cursor
    if cursor.get("format") == FRONTIER_FORMAT:
        bound_cursor = {"format": FRONTIER_REF_FORMAT, "stage": entry}
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
        slot.isoformat(),
        not_before,
        bound_requests,
        bound_cursor,
        (*state.pending, entry),
    )
    put_private(store, source_key(state.source), candidate.encoded(), previous_etag=expected_etag)
    return entry


def load_frontier_cursor(store: ObjectStore, state: ArticlePlanning) -> dict[str, Any]:
    """Read one exact workset before HTTP, retaining old cursors for migration."""
    cursor = state.cursor
    if cursor.get("format") != FRONTIER_REF_FORMAT:
        return cursor
    entry = cursor["stage"]
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
    if report["source"] != state.source or frontier.get("format") != FRONTIER_FORMAT:
        raise ValueError("article frontier source/format mismatch")
    return {
        **frontier,
        "indexes": [
            {**item, "stage": entry} if item["stage"].get("stage_id") == entry["stage_id"] else item
            for item in frontier["indexes"]
        ],
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
    candidates = list(state.pending)
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
