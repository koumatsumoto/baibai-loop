"""Query-only shortlist views."""

from __future__ import annotations

import json
from pathlib import Path

from .sqlite import read_rows

# Both queries must name the same newest shortlist, so they share one total order.
_SELECT = "SELECT payload FROM shortlist ORDER BY as_of DESC, published_at DESC, shortlist_id DESC"
_SELECT_BY_ID = "SELECT payload FROM shortlist WHERE shortlist_id = ?"
_SELECT_BY_SELECTION = (
    "SELECT payload FROM shortlist WHERE selection_id = ? "
    "ORDER BY published_at DESC, shortlist_id DESC"
)


def list_shortlist_payloads(path: Path) -> list[dict[str, object]]:
    return [_payload(row[0]) for row in read_rows(path, _SELECT)]


def shortlist_payload(path: Path, shortlist_id: str) -> dict[str, object] | None:
    """Return one named shortlist, or ``None`` when this store has no such judgment.

    ``research prepare`` binds a workspace to the Research Gate judgment named here,
    so the caller has to tell "no such shortlist" apart from a shortlist it may not
    use. The projection is the same one the history and Web views read: the version
    stays in the payload, and deciding which versions may bind research belongs to
    the research boundary, not to this query.
    """

    rows = read_rows(path, _SELECT_BY_ID, (shortlist_id,))
    return _payload(rows[0][0]) if rows else None


def shortlist_payloads_for_selection(path: Path, selection_id: str) -> list[dict[str, object]]:
    """Return every canonical judgment made over one selection, newest first.

    Publication allows only one — a second shortlist over the same selection carries
    a Review Basis that is stale by then — so a caller that needs *the* judgment for
    a cycle asks by selection and treats any other count as a store it must not
    interpret. Asking by selection rather than by name is what keeps the judgment
    anchored to the machine inputs instead of to whatever ID a caller supplies.
    """

    return [_payload(row[0]) for row in read_rows(path, _SELECT_BY_SELECTION, (selection_id,))]


def latest_shortlist_payload(path: Path) -> dict[str, object] | None:
    """Return the shortlist `baibai-web` shows, without loading canonical history."""

    rows = read_rows(path, f"{_SELECT} LIMIT 1")
    return _payload(rows[0][0]) if rows else None


def _payload(raw: object) -> dict[str, object]:
    payload = json.loads(str(raw))
    if not isinstance(payload, dict):
        raise ValueError("shortlist payload must be an object")
    version = payload.get("schema_version")
    if version == 5:
        return _project_v5(payload)
    if version in {2, 3, 4}:
        return _project_legacy(payload)
    raise ValueError(f"unsupported shortlist schema_version: {version!r}")


def _project_v5(payload: dict[str, object]) -> dict[str, object]:
    projected = dict(payload)
    projected["attention_provenance_status"] = "exact"
    projected["entries"] = _project_entries(payload.get("entries"), legacy=False)
    return projected


def _project_legacy(payload: dict[str, object]) -> dict[str, object]:
    """Project known immutable v2-v4 history without inventing exact identities."""

    projected = dict(payload)
    projected["attention_policy_id"] = None
    projected["attention_policy_hash"] = None
    projected["attention_policy_parameters"] = None
    projected["review_basis_shortlist_id"] = None
    projected["research_gate_contract_id"] = None
    projected["attention_provenance_status"] = "unresolved"
    projected["entries"] = _project_entries(payload.get("entries"), legacy=True)
    return projected


def _project_entries(value: object, *, legacy: bool) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("shortlist entries must be a list")
    entries: list[object] = []
    for value_entry in value:
        if not isinstance(value_entry, dict):
            raise ValueError("shortlist entry must be an object")
        entry = dict(value_entry)
        snapshot_value = entry.get("machine_snapshot")
        if isinstance(snapshot_value, dict):
            snapshot = dict(snapshot_value)
            if legacy:
                snapshot["opportunity_lane_id"] = "value-carry"
                snapshot["selection_policy_id"] = None
                snapshot["selection_policy_hash"] = None
                snapshot["lane_rank"] = snapshot.get("rank")
                snapshot["lane_native_value"] = entry.get("er_annual")
                snapshot["lane_native_unit"] = "annual_ratio"
                snapshot["baseline_er_rank"] = snapshot.get("rank")
                snapshot["primary_evidence_pattern_id"] = snapshot.get("screening_playbook")
                snapshot["policy_diagnostic_ids"] = None
                snapshot["lane_provenance_status"] = "legacy_inferred"
            else:
                snapshot["lane_provenance_status"] = "exact"
            entry["machine_snapshot"] = snapshot
        elif legacy:
            entry["lane_provenance_status"] = "unresolved"
        entries.append(entry)
    return entries


__all__ = [
    "latest_shortlist_payload",
    "list_shortlist_payloads",
    "shortlist_payload",
    "shortlist_payloads_for_selection",
]
