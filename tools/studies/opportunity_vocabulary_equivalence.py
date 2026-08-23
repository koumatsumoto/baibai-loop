"""Compare pre/post vocabulary screening artifacts after expected wire renames."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml

from baibai_engine.foundation.filesystem import write_text_atomic

_NEW_LONGLIST_PROVENANCE = {
    "opportunity_lane_id",
    "selection_policy_id",
    "selection_policy_hash",
    "lane_rank",
    "lane_native_value",
    "lane_native_unit",
    "baseline_er_rank",
    "policy_diagnostic_ids",
}


def _read_candidates(path: Path, run_revision_id: str) -> list[dict[str, object]]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT payload FROM screening_candidate WHERE run_revision_id = ? ORDER BY ordinal",
            (run_revision_id,),
        ).fetchall()
    return [json.loads(str(row[0])) for row in rows]


def _read_run(path: Path, run_revision_id: str) -> dict[str, object]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        row = connection.execute(
            "SELECT payload FROM screening_run WHERE run_revision_id = ?",
            (run_revision_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"run not found: {run_revision_id}")
    return json.loads(str(row[0]))


def _read_selection(path: Path, selection_id: str) -> dict[str, object]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        row = connection.execute(
            "SELECT payload FROM screening_selection WHERE selection_id = ?",
            (selection_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"selection not found: {selection_id}")
    return json.loads(str(row[0]))


def _normalize_candidate(value: Mapping[str, object]) -> dict[str, object]:
    normalized = dict(value)
    hits = []
    for raw in _mapping_items(value.get("evidence_hits")):
        hit = dict(raw)
        if "playbook_id" in hit:
            hit["evidence_pattern_id"] = hit.pop("playbook_id")
        hits.append(hit)
    normalized["evidence_hits"] = hits
    return normalized


def _normalize_selection_row(
    value: Mapping[str, object], *, drop_new_provenance: bool
) -> dict[str, object]:
    normalized = dict(value)
    if "selection_playbook" in normalized:
        normalized["primary_evidence_pattern_id"] = normalized.pop("selection_playbook")
    if "screening_playbook" in normalized:
        normalized["primary_evidence_pattern_id"] = normalized.pop("screening_playbook")
    if drop_new_provenance:
        for field in _NEW_LONGLIST_PROVENANCE:
            normalized.pop(field, None)
    return normalized


def _mapping_items(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def compare(
    *,
    old_db: Path,
    old_run_id: str,
    old_selection_id: str,
    new_db: Path,
    new_run_id: str,
    new_selection_id: str,
) -> dict[str, object]:
    old_candidates_raw = _read_candidates(old_db, old_run_id)
    new_candidates_raw = _read_candidates(new_db, new_run_id)
    old_candidates = [_normalize_candidate(item) for item in old_candidates_raw]
    new_candidates = [_normalize_candidate(item) for item in new_candidates_raw]
    old_by_ticker = {str(item["ticker"]): item for item in old_candidates}
    new_by_ticker = {str(item["ticker"]): item for item in new_candidates}
    common = sorted(old_by_ticker.keys() & new_by_ticker.keys())
    candidate_mismatches = [
        ticker for ticker in common if old_by_ticker[ticker] != new_by_ticker[ticker]
    ]
    estimate_fields = (
        "er_annual",
        "er_reversion_annual",
        "er_carry_annual",
        "fv_sector_median_yen",
        "fv_self_range_yen",
    )
    estimate_mismatches = [
        ticker
        for ticker in common
        if any(
            old_by_ticker[ticker].get(field) != new_by_ticker[ticker].get(field)
            for field in estimate_fields
        )
    ]

    old_selection = _read_selection(old_db, old_selection_id)
    new_selection = _read_selection(new_db, new_selection_id)
    old_recommendations = [
        _normalize_selection_row(item, drop_new_provenance=False)
        for item in _mapping_items(old_selection.get("recommendations"))
    ]
    new_recommendations = [
        _normalize_selection_row(item, drop_new_provenance=False)
        for item in _mapping_items(new_selection.get("recommendations"))
    ]
    old_longlist = [
        _normalize_selection_row(item, drop_new_provenance=False)
        for item in _mapping_items(old_selection.get("longlist"))
    ]
    new_longlist = [
        _normalize_selection_row(item, drop_new_provenance=True)
        for item in _mapping_items(new_selection.get("longlist"))
    ]
    old_run = _read_run(old_db, old_run_id)
    new_run = _read_run(new_db, new_run_id)
    checks = {
        "candidate_membership": old_by_ticker.keys() == new_by_ticker.keys(),
        "candidate_order": [item["ticker"] for item in old_candidates]
        == [item["ticker"] for item in new_candidates],
        "candidate_payload_after_field_alias": not candidate_mismatches,
        "evidence_hit_meaning": all(
            old_by_ticker[ticker].get("evidence_hits") == new_by_ticker[ticker].get("evidence_hits")
            for ticker in common
        ),
        "estimates_and_fv": not estimate_mismatches,
        "recommendation": old_recommendations == new_recommendations,
        "longlist_order_and_values": old_longlist == new_longlist,
        "candidate_diagnostic_projection": [
            (row.get("ticker"), row.get("durability_rating"), row.get("durability_caution_reasons"))
            for row in old_recommendations
        ]
        == [
            (row.get("ticker"), row.get("durability_rating"), row.get("durability_caution_reasons"))
            for row in new_recommendations
        ],
    }
    return {
        "kind": "opportunity-vocabulary-semantic-equivalence",
        "as_of": old_run.get("asof_date"),
        "old": {
            "source": "pre-rename implementation against the current local market store",
            "run_revision_id": old_run_id,
            "selection_id": old_selection_id,
            "rules_hash": old_run.get("screening_rules_hash"),
        },
        "new": {
            "source": "post-rename implementation against the same current local market store",
            "run_revision_id": new_run_id,
            "selection_id": new_selection_id,
            "rules_hash": new_run.get("screening_rules_hash"),
        },
        "expected_differences": [
            "rules_hash",
            "bundle_identity",
            "Evidence Pattern and Candidate Diagnostic field/class names",
            "Value / Carry and Attention exact provenance",
        ],
        "counts": {
            "old_candidates": len(old_candidates),
            "new_candidates": len(new_candidates),
            "candidate_payload_mismatches": len(candidate_mismatches),
            "estimate_or_fv_mismatches": len(estimate_mismatches),
        },
        "checks": checks,
        "verdict": "equivalent" if all(checks.values()) else "different",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-db", type=Path, required=True)
    parser.add_argument("--old-run-id", required=True)
    parser.add_argument("--old-selection-id", required=True)
    parser.add_argument("--new-db", type=Path, required=True)
    parser.add_argument("--new-run-id", required=True)
    parser.add_argument("--new-selection-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = compare(
        old_db=args.old_db,
        old_run_id=args.old_run_id,
        old_selection_id=args.old_selection_id,
        new_db=args.new_db,
        new_run_id=args.new_run_id,
        new_selection_id=args.new_selection_id,
    )
    write_text_atomic(args.output, yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
    print(f"wrote {args.output}: verdict={payload['verdict']}")
    return 0 if payload["verdict"] == "equivalent" else 1


if __name__ == "__main__":
    raise SystemExit(main())
