from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from tools.studies.opportunity_vocabulary_equivalence import compare


def _write_store(
    path: Path,
    *,
    run_id: str,
    selection_id: str,
    candidate: dict[str, object],
    selection: dict[str, object],
) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE screening_candidate (
                run_revision_id TEXT, ordinal INTEGER, payload TEXT
            );
            CREATE TABLE screening_run (run_revision_id TEXT, payload TEXT);
            CREATE TABLE screening_selection (selection_id TEXT, payload TEXT);
            """
        )
        connection.execute(
            "INSERT INTO screening_candidate VALUES (?, 0, ?)",
            (run_id, json.dumps(candidate)),
        )
        connection.execute(
            "INSERT INTO screening_run VALUES (?, ?)",
            (run_id, json.dumps({"asof_date": "2026-08-21"})),
        )
        connection.execute(
            "INSERT INTO screening_selection VALUES (?, ?)",
            (selection_id, json.dumps(selection)),
        )


def _candidate(*, old: bool, er_annual: float = 0.1) -> dict[str, object]:
    hit_id = "playbook_id" if old else "evidence_pattern_id"
    return {
        "ticker": "2331",
        "metrics": {
            "er_annual": er_annual,
            "er_reversion_annual": 0.08,
            "er_carry_annual": 0.02,
            "fv_sector_median_yen": 1200.0,
            "fv_self_range_yen": 1100.0,
        },
        "evidence_hits": [{hit_id: "valuation-reversion"}],
    }


def _selection(*, old: bool) -> dict[str, object]:
    field = "selection_playbook" if old else "primary_evidence_pattern_id"
    row: dict[str, object] = {"ticker": "2331", field: "valuation-reversion"}
    if not old:
        row.update(
            {
                "opportunity_lane_id": "value-carry",
                "selection_policy_id": "value-carry-v1",
                "selection_policy_hash": "a" * 64,
                "lane_rank": 1,
                "lane_native_value": 0.1,
                "lane_native_unit": "annual_ratio",
                "baseline_er_rank": 1,
                "policy_diagnostic_ids": [],
            }
        )
    return {"recommendations": [{"ticker": "2331"}], "longlist": [row]}


def _compare(old_db: Path, new_db: Path) -> dict[str, object]:
    return compare(
        old_db=old_db,
        old_run_id="old-run",
        old_selection_id="old-selection",
        old_implementation_git_commit="a" * 40,
        old_calibration_bundle_id="old-bundle",
        old_calibration_bundle_manifest_sha256="b" * 64,
        new_db=new_db,
        new_run_id="new-run",
        new_selection_id="new-selection",
        new_implementation_git_commit="c" * 40,
        new_calibration_bundle_id="new-bundle",
        new_calibration_bundle_manifest_sha256="d" * 64,
    )


def test_nested_estimate_or_fv_change_is_counted_explicitly(tmp_path: Path) -> None:
    old_db = tmp_path / "old.sqlite"
    new_db = tmp_path / "new.sqlite"
    _write_store(
        old_db,
        run_id="old-run",
        selection_id="old-selection",
        candidate=_candidate(old=True),
        selection=_selection(old=True),
    )
    _write_store(
        new_db,
        run_id="new-run",
        selection_id="new-selection",
        candidate=_candidate(old=False, er_annual=0.2),
        selection=_selection(old=False),
    )

    result = _compare(old_db, new_db)

    assert result["counts"]["estimate_or_fv_mismatches"] == 1  # type: ignore[index]
    assert result["checks"]["estimates_and_fv"] is False  # type: ignore[index]
    assert result["verdict"] == "different"


def test_malformed_selection_row_fails_closed(tmp_path: Path) -> None:
    old_db = tmp_path / "old.sqlite"
    new_db = tmp_path / "new.sqlite"
    _write_store(
        old_db,
        run_id="old-run",
        selection_id="old-selection",
        candidate=_candidate(old=True),
        selection={"recommendations": ["not-a-row"], "longlist": []},
    )
    _write_store(
        new_db,
        run_id="new-run",
        selection_id="new-selection",
        candidate=_candidate(old=False),
        selection=_selection(old=False),
    )

    with pytest.raises(ValueError, match="old recommendations contains an invalid row"):
        _compare(old_db, new_db)
