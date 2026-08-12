from __future__ import annotations

import copy
import io
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from tests.helpers.macro_context import macro_context_payload
from tools.experiments.macro_world_model.build_evidence_snapshot import (
    build_snapshot,
    canonical_payload_sha256,
)
from tools.experiments.macro_world_model.render_report import render_report
from tools.experiments.macro_world_model.validate_world_model import (
    WorldModelValidationError,
    _canonical_v4_document,
    _reject_prior_material,
    _required_one_page_paths,
    _required_v4_paths,
    build_freeze_record,
    validate_workspace,
)
from tools.experiments.macro_world_model.validate_world_model import (
    main as validate_world_model_main,
)


def _macro_store(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE observations (
                series_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                value REAL NOT NULL,
                unit TEXT NOT NULL,
                vintage_at TEXT NOT NULL,
                fetch_status TEXT NOT NULL,
                source_url TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("a", "2026-07-01", 1.0, "index", "2026-07-02", "ok", "https://a/1"),
                ("a", "2026-08-01", 2.0, "index", "2026-08-02", "ok", "https://a/2"),
                ("a", "2026-08-01", 2.5, "index", "2026-08-03", "ok", "https://a/3"),
                ("b", "2026-07-01", 5.0, "index", "2026-07-02", "ok", "https://b/1"),
                ("b", "2026-08-01", 5.0, "index", "2026-08-02", "ok", "https://b/2"),
            ],
        )


def _reading() -> dict[str, Any]:
    return {
        "asof": "2026-08-07",
        "rules_revision": "fixture-r1",
        "series": [
            {
                "series_id": "a",
                "name": "Series A",
                "category": "growth",
                "geography": "US",
                "frequency": "monthly",
                "unit": "index",
                "latest_value": 2.5,
                "observed_at": "2026-08-01",
                "stale": False,
                "insufficient_history": False,
                "flags": [],
                "z_score": 0.2,
                "percentile": 0.6,
                "window_years": 3,
                "window_observations": 36,
                "expected_observations": 36,
                "statistic": "yoy",
                "statistic_unit": "percent",
                "statistic_value": 2.5,
                "next_print_estimate": "2026-09-01",
                "print_due_in_days": 25,
                "short_trend": {"direction": "up"},
                "long_trend": {"direction": "down"},
            },
            {
                "series_id": "b",
                "name": "Series B",
                "category": "inflation",
                "geography": "JP",
                "frequency": "monthly",
                "unit": "index",
                "latest_value": 5.0,
                "observed_at": "2026-08-01",
                "stale": False,
                "insufficient_history": False,
                "flags": [],
                "z_score": 0.0,
                "percentile": 0.5,
                "window_years": 10,
                "window_observations": 120,
                "expected_observations": 120,
                "statistic": "level",
                "statistic_unit": "index",
                "statistic_value": 5.0,
                "next_print_estimate": "2026-09-01",
                "print_due_in_days": 25,
                "short_trend": {"direction": "flat"},
                "long_trend": {"direction": "flat"},
            },
        ],
    }


def _coverage() -> dict[str, Any]:
    return {
        "standing_coverage": [
            {"series_id": "b", "block": "Japan"},
            {"series_id": "a", "block": "United States"},
        ],
        "analyst_additions": [{"series_id": "b", "reason": "portfolio exposure"}],
        "decisions": [
            {"series_id": "a", "status": "selected"},
            {"series_id": "b", "status": "excluded", "reason": "unchanged release"},
        ],
    }


def test_snapshot_hash_is_idempotent_and_order_invariant(tmp_path: Path) -> None:
    store = tmp_path / "macro.sqlite"
    _macro_store(store)
    first = build_snapshot(
        reading=_reading(),
        macro_db=store,
        coverage_config=_coverage(),
        created_at=datetime(2026, 8, 8, tzinfo=UTC),
        previous_as_of=date(2026, 7, 31),
    )
    reversed_reading = _reading()
    reversed_reading["series"].reverse()
    reversed_coverage = _coverage()
    reversed_coverage["standing_coverage"].reverse()
    reversed_coverage["decisions"].reverse()
    second = build_snapshot(
        reading=reversed_reading,
        macro_db=store,
        coverage_config=reversed_coverage,
        created_at=datetime(2026, 8, 9, tzinfo=UTC),
        previous_as_of=date(2026, 7, 31),
    )

    expected = first["canonical_payload_sha256"]
    assert expected == second["canonical_payload_sha256"]
    assert expected == canonical_payload_sha256(first)
    assert expected == canonical_payload_sha256(second)
    series_a = cast(list[dict[str, Any]], first["coverage_scan"])[0]
    assert series_a["unit"] == "index"
    assert series_a["statistic"] == "yoy"
    assert series_a["statistic_unit"] == "percent"
    assert series_a["window_years"] == 3
    assert series_a["next_print_estimate"] == "2026-09-01"


def test_snapshot_rejects_missing_reading_semantics(tmp_path: Path) -> None:
    store = tmp_path / "macro.sqlite"
    _macro_store(store)
    reading = _reading()
    del reading["series"][0]["statistic_unit"]

    with pytest.raises(ValueError, match="missing semantic fields"):
        build_snapshot(reading=reading, macro_db=store)


def test_snapshot_detects_same_observed_at_vintages_as_revision(tmp_path: Path) -> None:
    store = tmp_path / "macro.sqlite"
    _macro_store(store)
    snapshot = build_snapshot(reading=_reading(), macro_db=store)

    scan = cast(list[dict[str, Any]], snapshot["coverage_scan"])
    series_a = next(row for row in scan if row["series_id"] == "a")
    assert series_a["revisions"] == [
        {
            "observed_at": "2026-08-01",
            "derived_recompute": False,
            "vintages": [
                {
                    "value": 2.0,
                    "unit": "index",
                    "vintage_at": "2026-08-02",
                    "fetch_status": "ok",
                    "source_url": "https://a/2",
                },
                {
                    "value": 2.5,
                    "unit": "index",
                    "vintage_at": "2026-08-03",
                    "fetch_status": "ok",
                    "source_url": "https://a/3",
                },
            ],
        }
    ]
    assert series_a["revisions_truncated"] == 0
    assert "recent_revision" in series_a["machine_materiality_reasons"]


def test_snapshot_bounds_revision_history_without_changing_materiality(tmp_path: Path) -> None:
    store = tmp_path / "macro.sqlite"
    with sqlite3.connect(store) as connection:
        connection.execute(
            """
            CREATE TABLE observations (
                series_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                value REAL NOT NULL,
                unit TEXT NOT NULL,
                vintage_at TEXT NOT NULL,
                fetch_status TEXT NOT NULL,
                source_url TEXT NOT NULL
            )
            """
        )
        rows = [
            ("windowed", "2024-08-06", 1.0, "index", "2026-08-01", "ok", "https://old/1"),
            ("windowed", "2024-08-06", 2.0, "index", "2026-08-02", "ok", "https://old/2"),
            ("windowed", "2026-07-31", 3.0, "index", "2026-07-29", "ok", "https://early/1"),
            ("windowed", "2026-07-31", 4.0, "index", "2026-07-30", "ok", "https://early/2"),
            ("windowed", "2026-08-01", 5.0, "index", "2026-07-29", "ok", "https://material/1"),
            ("windowed", "2026-08-01", 6.0, "index", "2026-07-30", "ok", "https://material/2"),
            (
                "windowed",
                "2026-07-30",
                7.0,
                "index",
                "2026-07-30",
                "ok",
                "https://github.com/koumatsumoto/baibai-loop/blob/main/engine/src/formulas.py",
            ),
            (
                "windowed",
                "2026-07-30",
                8.0,
                "index",
                "2026-08-01",
                "ok",
                "https://github.com/koumatsumoto/baibai-loop/blob/main/engine/src/formulas.py",
            ),
        ]
        for index in range(22):
            observed_at = date(2026, 6, 1) + timedelta(days=index)
            rows.extend(
                [
                    (
                        "capped",
                        observed_at.isoformat(),
                        float(index),
                        "index",
                        "2026-07-30T00:00:00+00:00",
                        "ok",
                        f"https://cap/{index}/1",
                    ),
                    (
                        "capped",
                        observed_at.isoformat(),
                        float(index + 1),
                        "index",
                        f"2026-08-01T00:{index:02d}:00+00:00",
                        "ok",
                        f"https://cap/{index}/2",
                    ),
                ]
            )
        connection.executemany("INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?)", rows)

    reading = {
        "asof": "2026-08-07",
        "rules_revision": "fixture-r1",
        "series": [
            {
                "series_id": series_id,
                "latest_value": latest_value,
                "observed_at": observed_at,
                "window_years": 3,
                "window_observations": 36,
                "expected_observations": 36,
                "statistic": "level",
                "statistic_unit": "index",
                "statistic_value": latest_value,
                "next_print_estimate": "2026-09-01",
                "print_due_in_days": 25,
                "short_trend": {"direction": "flat"},
                "long_trend": {"direction": "flat"},
            }
            for series_id, latest_value, observed_at in (
                ("windowed", 6.0, "2026-08-01"),
                ("capped", 22.0, "2026-06-22"),
            )
        ],
    }
    snapshot = build_snapshot(
        reading=reading,
        macro_db=store,
        previous_as_of=date(2026, 7, 31),
        created_at=datetime(2026, 8, 8, tzinfo=UTC),
    )
    scan = {row["series_id"]: row for row in cast(list[dict[str, Any]], snapshot["coverage_scan"])}

    windowed = scan["windowed"]
    assert [row["observed_at"] for row in windowed["revisions"]] == ["2026-07-30"]
    assert windowed["revisions"][0]["derived_recompute"] is True
    assert windowed["revisions_truncated"] == 0
    assert "recent_revision" in windowed["machine_materiality_reasons"]

    capped = scan["capped"]
    assert len(capped["revisions"]) == 20
    assert capped["revisions_truncated"] == 2
    assert capped["revisions"][0]["observed_at"] == "2026-06-22"
    assert capped["revisions"][-1]["observed_at"] == "2026-06-03"
    assert snapshot["materiality_policy_revision"] == "stage-a-v2"
    assert snapshot["revision_window"] == {
        "observed_at_start": "2024-08-07",
        "vintage_at_start": "2026-07-31",
        "vintage_at_start_source": "previous_head_as_of",
        "max_revisions_per_series": 20,
    }


def test_snapshot_uses_ninety_day_revision_fallback(tmp_path: Path) -> None:
    store = tmp_path / "macro.sqlite"
    _macro_store(store)

    snapshot = build_snapshot(reading=_reading(), macro_db=store)

    revision_window = cast(dict[str, Any], snapshot["revision_window"])
    assert revision_window["vintage_at_start"] == "2026-05-09"
    assert revision_window["vintage_at_start_source"] == "fallback_90_days"


def test_snapshot_rejects_decision_for_non_candidate(tmp_path: Path) -> None:
    store = tmp_path / "macro.sqlite"
    _macro_store(store)
    coverage = {"decisions": [{"series_id": "b", "status": "excluded", "reason": "not material"}]}

    with pytest.raises(ValueError, match="non-candidates"):
        build_snapshot(reading=_reading(), macro_db=store, coverage_config=coverage)


def _workspace_documents() -> dict[str, dict[str, Any]]:
    evidence_id = "series:a"
    documents: dict[str, dict[str, Any]] = {
        "charter.yaml": {
            "as_of": "2026-08-07",
            "data_cutoff": "2026-08-07T23:59:59Z",
            "central_questions": [f"Central question {number}" for number in range(1, 7)],
            "horizons": ["now", "0_3m", "3_12m", "12_24m"],
            "conditioning_assumptions": ["No unobserved policy discontinuity."],
            "standing_questions": [
                "JPY exposure",
                "JGB and discount rates",
                "US demand and AI capex",
                "China proxies",
                "Energy exposure",
            ],
        },
        "evidence-snapshot.json": {
            "schema_version": 2,
            "as_of": "2026-08-07",
            "coverage_scan": [
                {
                    "evidence_id": evidence_id,
                    "series_id": "a",
                    "unit": "index",
                    "latest_value": 2.5,
                    "observed_at": "2026-08-01",
                    "window_years": 3,
                    "window_observations": 36,
                    "expected_observations": 36,
                    "statistic": "yoy",
                    "statistic_unit": "percent",
                    "statistic_value": 2.5,
                    "next_print_estimate": "2026-09-01",
                    "print_due_in_days": 25,
                }
            ],
            "coverage_manifest": [
                {"candidate_id": evidence_id, "series_id": "a", "status": "selected"}
            ],
        },
        "evidence-packs.yaml": {
            "external_sources": [],
            "packs": [
                {
                    "pack_id": "us-demand",
                    "block": "US growth",
                    "evidence_for": [evidence_id],
                    "evidence_against": [evidence_id],
                    "open_questions": ["Does the next release confirm the direction?"],
                }
            ],
        },
        "states.yaml": {
            "states": [
                {
                    "state_id": "us_growth",
                    "geography": "US",
                    "block": "growth",
                    "reference_frame": "relative to trend",
                    "horizons": {
                        horizon: {
                            "level": "near trend",
                            "momentum": "slowing",
                            "acceleration": "negative",
                            "breadth": "mixed",
                            "persistence": "medium",
                        }
                        for horizon in ("now", "0_3m", "3_12m", "12_24m")
                    },
                    "evidence_for": [evidence_id],
                    "evidence_against": [evidence_id],
                    "uncertainty": {
                        "data": "medium",
                        "state": "medium",
                        "structural": "high",
                        "policy": "high",
                    },
                }
            ]
        },
        "hypotheses.yaml": {
            "hypotheses": [
                {
                    "hypothesis_id": hypothesis_id,
                    "mechanism_summary": f"Mechanism {rank}",
                    "plausibility_rank": rank,
                    "evidence_for": [evidence_id],
                    "evidence_against": [evidence_id],
                    "unexplained_residuals": ["Residual"],
                    "required_assumptions": ["Assumption"],
                    "discriminating_signposts": ["Signpost"],
                    "invalidation": ["Invalidation"],
                }
                for rank, hypothesis_id in enumerate(("h1", "h2"), start=1)
            ]
        },
        "evidence-matrix.yaml": {
            "rows": [
                {
                    "evidence_id": evidence_id,
                    "hypothesis_assessments": {"h1": "supports", "h2": "contradicts"},
                }
            ]
        },
        "world-model.yaml": {
            "schema_version": 2,
            "as_of": "2026-08-07",
            "graph": {
                "nodes": [
                    {"node_id": "n1", "node_type": "observed_state", "label": "Demand"},
                    {"node_id": "n2", "node_type": "real_outcome", "label": "Growth"},
                ],
                "edges": [
                    {
                        "edge_id": "e1",
                        "from_node": "n1",
                        "to_node": "n2",
                        "relation_kind": "judgmental_hypothesis",
                        "claim_strength": "plausible",
                        "sign": "positive",
                        "lag": {"min_months": 0, "max_months": 3},
                        "evidence_ids": [evidence_id],
                        "falsifiers": [{"kind": "series", "name": "Series A"}],
                    }
                ],
            },
            "key_judgments": [
                {
                    "judgment_id": f"j{number}",
                    "summary": f"Judgment {number}",
                    "state_ids": ["us_growth"],
                    "node_ids": ["n1", "n2"],
                    "edge_ids": ["e1"],
                    "horizons": ["now", "0_3m"],
                    "counter_hypothesis_ids": ["h2"],
                    "evidence_ids": [evidence_id],
                }
                for number in range(1, 4)
            ],
            "baseline_path": {
                horizon: [f"Path for {horizon}"] for horizon in ("now", "0_3m", "3_12m", "12_24m")
            },
            "scenarios": [
                {
                    "scenario_id": scenario_id,
                    "name": f"Mechanism {rank}: demand path",
                    "plausibility_rank": rank,
                    "initial_shock": "Demand changes.",
                    "persistence": "The change persists.",
                    "propagation_delta": "Credit transmits the change.",
                    "policy_reaction": "Policy responds with a lag.",
                    "paths": dict.fromkeys(
                        ("growth", "inflation", "rates", "credit", "fx", "balance_sheet"),
                        "Named directional path",
                    ),
                    "signposts": ["Named release"],
                    "invalidation": ["Opposite release result"],
                    "known_omissions": ["Fiscal discontinuity"],
                    "state_ids": ["us_growth"],
                    "hypothesis_ids": ["h1"],
                    "node_ids": ["n1", "n2"],
                    "edge_ids": ["e1"],
                    "evidence_ids": [evidence_id],
                }
                for rank, scenario_id in enumerate(("baseline", "downside", "upside"), start=1)
            ],
            "unresolved_tensions": ["Demand level and momentum diverge."],
            "signposts": ["Series A next release"],
        },
    }
    snapshot = documents["evidence-snapshot.json"]
    snapshot["canonical_payload_sha256"] = canonical_payload_sha256(snapshot)
    return documents


def _write_workspace(path: Path, documents: dict[str, dict[str, Any]]) -> None:
    path.mkdir()
    for name, document in documents.items():
        (path / name).write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def test_validator_accepts_contract_and_checks_blind_freeze(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_workspace(workspace, _workspace_documents())
    freeze = tmp_path / "blind-freeze.json"
    freeze.write_text(json.dumps(build_freeze_record(workspace)), encoding="utf-8")

    result = validate_workspace(workspace, freeze_path=freeze)

    assert result["status"] == "ok"
    assert result["blind_freeze_sha256"]


def test_blind_scan_accepts_yaml_date_scalars() -> None:
    _reject_prior_material({"charter": {"as_of": date(2026, 8, 7)}})


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("missing_falsifier", "observable falsifier"),
        ("node_cap", "hard cap of 20 nodes"),
        ("unknown_evidence", "unknown evidence"),
        ("invalid_state_dimension", "level is required"),
        ("invalid_snapshot_hash", "canonical hash is invalid"),
        ("missing_snapshot_semantics", "missing reading semantic fields"),
        ("invalid_snapshot_window", "window_years must be positive"),
        ("inconsistent_snapshot_print", "does not match snapshot as_of"),
        ("mismatched_snapshot_as_of", "as_of must match charter.as_of"),
        ("uncovered_selected", "absent from evidence packs"),
        ("missing_scenario_lineage", "scenario baseline.state_ids"),
        ("missing_bull_scenario", "requires exactly 3 scenarios"),
        ("prior_material", "prior macro context id"),
    ],
)
def test_validator_rejects_guard_violations(tmp_path: Path, case: str, expected: str) -> None:
    documents = copy.deepcopy(_workspace_documents())
    if case == "missing_falsifier":
        documents["world-model.yaml"]["graph"]["edges"][0]["falsifiers"] = []
    elif case == "node_cap":
        documents["world-model.yaml"]["graph"]["nodes"] = [
            {"node_id": f"n{number}", "node_type": "observed_state", "label": "Node"}
            for number in range(1, 22)
        ]
    elif case == "unknown_evidence":
        documents["states.yaml"]["states"][0]["evidence_for"] = ["series:missing"]
    elif case == "invalid_state_dimension":
        documents["states.yaml"]["states"][0]["horizons"]["now"]["level"] = 42
    elif case == "invalid_snapshot_hash":
        documents["evidence-snapshot.json"]["canonical_payload_sha256"] = "invalid"
    elif case == "missing_snapshot_semantics":
        snapshot = documents["evidence-snapshot.json"]
        del snapshot["coverage_scan"][0]["statistic_unit"]
        snapshot["canonical_payload_sha256"] = canonical_payload_sha256(snapshot)
    elif case == "invalid_snapshot_window":
        snapshot = documents["evidence-snapshot.json"]
        snapshot["coverage_scan"][0]["window_years"] = 0
        snapshot["canonical_payload_sha256"] = canonical_payload_sha256(snapshot)
    elif case == "inconsistent_snapshot_print":
        snapshot = documents["evidence-snapshot.json"]
        snapshot["coverage_scan"][0]["print_due_in_days"] = 24
        snapshot["canonical_payload_sha256"] = canonical_payload_sha256(snapshot)
    elif case == "mismatched_snapshot_as_of":
        snapshot = documents["evidence-snapshot.json"]
        snapshot["as_of"] = "2026-08-08"
        snapshot["coverage_scan"][0]["print_due_in_days"] = 24
        snapshot["canonical_payload_sha256"] = canonical_payload_sha256(snapshot)
    elif case == "uncovered_selected":
        snapshot = documents["evidence-snapshot.json"]
        extra_evidence = copy.deepcopy(snapshot["coverage_scan"][0])
        extra_evidence.update({"evidence_id": "series:b", "series_id": "b"})
        snapshot["coverage_scan"].append(extra_evidence)
        snapshot["coverage_manifest"].append(
            {"candidate_id": "series:b", "series_id": "b", "status": "selected"}
        )
        snapshot["canonical_payload_sha256"] = canonical_payload_sha256(snapshot)
    elif case == "missing_scenario_lineage":
        del documents["world-model.yaml"]["scenarios"][0]["state_ids"]
    elif case == "missing_bull_scenario":
        documents["world-model.yaml"]["scenarios"].pop()
    else:
        documents["charter.yaml"]["conditioning_assumptions"] = ["macro-context-2026-07-31-prior"]
    workspace = tmp_path / case
    _write_workspace(workspace, documents)

    with pytest.raises(WorldModelValidationError, match=expected):
        validate_workspace(workspace)


def test_validator_accepts_legitimate_null_snapshot_semantics(tmp_path: Path) -> None:
    documents = _workspace_documents()
    snapshot = documents["evidence-snapshot.json"]
    row = snapshot["coverage_scan"][0]
    row["expected_observations"] = None
    row["statistic_value"] = None
    row["next_print_estimate"] = None
    row["print_due_in_days"] = None
    snapshot["canonical_payload_sha256"] = canonical_payload_sha256(snapshot)
    workspace = tmp_path / "workspace"
    _write_workspace(workspace, documents)

    assert validate_workspace(workspace)["status"] == "ok"


def test_validator_rejects_freeze_drift_and_probability_order(tmp_path: Path) -> None:
    documents = _workspace_documents()
    projection = _v2_projection()
    projection["scenarios"][0]["probability"] = 0.3
    projection["scenarios"][1]["probability"] = 0.5
    inputs = _write_projection_inputs(tmp_path, documents=documents, projection=projection)

    with pytest.raises(WorldModelValidationError, match="contradicts"):
        validate_workspace(
            inputs[0],
            freeze_path=inputs[1],
            v4_projection=inputs[2],
            v4_document=inputs[3],
            revision_diff=inputs[4],
        )

    drifted_states = copy.deepcopy(documents["states.yaml"])
    drifted_states["states"][0]["uncertainty"]["data"] = "low"
    (inputs[0] / "states.yaml").write_text(
        json.dumps(drifted_states, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(WorldModelValidationError, match="digests do not match"):
        validate_workspace(inputs[0], freeze_path=inputs[1])


def _v4_document() -> dict[str, Any]:
    payload = macro_context_payload(
        context_id="macro-context-2026-08-07-honesty-fixture",
        as_of="2026-08-07",
        published_at="2026-08-08T09:00:00+09:00",
        scorecard_deadline="2026-12-31",
    )
    payload["summary"] = "Demand is slowing while investment remains firm."
    return payload


def _revision_diff(*, changes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "as_of": "2026-08-07",
        "changes": changes
        if changes is not None
        else [{"area": "Demand", "summary": "Breadth narrows."}],
    }


def _qualifier(*, strength: str = "plausible", units: list[str] | None = None) -> dict[str, Any]:
    return {
        "claim_strength": strength,
        "uncertainty": ["data", "state", "structural", "policy"],
        "basis": {
            "real_nominal": "nominal",
            "stock_flow": "flow",
            "observation_expectation": "observation",
        },
        "units": units or ["percent"],
    }


def _v4_target_paths() -> list[str]:
    document = _canonical_v4_document(_v4_document())
    return sorted(_required_v4_paths(document)[0])


def _one_page_target_paths() -> list[str]:
    return sorted(
        _required_one_page_paths(
            _workspace_documents()["world-model.yaml"],
            _revision_diff(),
            expected_as_of=date(2026, 8, 7),
        )[0]
    )


def _v2_projection() -> dict[str, Any]:
    targets = [
        {
            "artifact": artifact,
            "field_path": field_path,
            "output_qualifier": _qualifier(),
        }
        for artifact, field_paths in (
            ("v4", _v4_target_paths()),
            ("one_page", _one_page_target_paths()),
        )
        for field_path in field_paths
    ]
    return {
        "schema_version": 2,
        "as_of": "2026-08-07",
        "projection_note": "All three scenario mappings are direct.",
        "scenarios": [
            {
                "scenario_id": "baseline",
                "v4_case": "base",
                "plausibility_rank": 1,
                "probability": 0.5,
            },
            {
                "scenario_id": "downside",
                "v4_case": "bear",
                "plausibility_rank": 2,
                "probability": 0.3,
            },
            {
                "scenario_id": "upside",
                "v4_case": "bull",
                "plausibility_rank": 3,
                "probability": 0.2,
            },
        ],
        "material_claims": [
            {
                "claim_id": "demand-divergence",
                "summary": "Demand and investment diverge.",
                "upstream": {
                    "state_ids": ["us_growth"],
                    "hypothesis_ids": ["h1", "h2"],
                    "judgment_ids": ["j1", "j2", "j3"],
                    "scenario_ids": ["baseline", "downside", "upside"],
                    "node_ids": ["n1", "n2"],
                    "edge_ids": ["e1"],
                    "evidence": [
                        {
                            "evidence_id": "series:a",
                            "measure": "statistic_value",
                            "units": ["percent"],
                        }
                    ],
                },
                "source_qualifier": _qualifier(),
                "targets": targets,
            }
        ],
    }


def _write_projection_inputs(
    root: Path, *, documents: dict[str, dict[str, Any]], projection: dict[str, Any]
) -> tuple[Path, Path, Path, Path, Path]:
    workspace = root / "workspace"
    _write_workspace(workspace, documents)
    freeze = root / "blind-freeze.json"
    freeze.write_text(json.dumps(build_freeze_record(workspace)), encoding="utf-8")
    projection_path = root / "v4-projection.yaml"
    projection_path.write_text(json.dumps(projection), encoding="utf-8")
    v4_document = root / "final-v4.yaml"
    v4_document.write_text(json.dumps(_v4_document()), encoding="utf-8")
    revision_diff = root / "revision-diff.yaml"
    revision_diff.write_text(json.dumps(_revision_diff()), encoding="utf-8")
    return workspace, freeze, projection_path, v4_document, revision_diff


def test_validator_rejects_v1_schema_downgrade(tmp_path: Path) -> None:
    documents = _workspace_documents()
    snapshot = documents["evidence-snapshot.json"]
    snapshot["schema_version"] = 1
    for field in (
        "window_years",
        "window_observations",
        "expected_observations",
        "statistic",
        "statistic_unit",
        "statistic_value",
        "next_print_estimate",
        "print_due_in_days",
    ):
        del snapshot["coverage_scan"][0][field]
    snapshot["canonical_payload_sha256"] = canonical_payload_sha256(snapshot)
    world_model = documents["world-model.yaml"]
    world_model["schema_version"] = 1
    for scenario in world_model["scenarios"]:
        for field in ("state_ids", "hypothesis_ids", "node_ids", "edge_ids", "evidence_ids"):
            del scenario[field]
    workspace = tmp_path / "workspace"
    _write_workspace(workspace, documents)

    with pytest.raises(WorldModelValidationError, match="restricted to the immutable"):
        validate_workspace(workspace)


@pytest.mark.parametrize("as_of", ["2026-08-07", "2026-08-12"])
def test_validator_accepts_frozen_v1_historical_workspaces(as_of: str) -> None:
    cycle = (
        Path(__file__).resolve().parents[2]
        / "reports"
        / "studies"
        / "2026-08-09-macro-world-model-pilot"
        / "cycles"
        / as_of
    )

    result = validate_workspace(cycle, freeze_path=cycle / "blind-freeze.json")

    assert result["workspace_schema_version"] == 1
    assert result["lineage_enforced"] is False


def test_check_cli_requires_all_v2_final_inputs(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_workspace(workspace, _workspace_documents())
    output = io.StringIO()

    exit_code = validate_world_model_main(["check", str(workspace)], stdout=output)

    assert exit_code == 1
    assert "schema v2 check requires final inputs" in output.getvalue()


def test_validator_rejects_projection_schema_downgrade(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_workspace(workspace, _workspace_documents())
    freeze = tmp_path / "blind-freeze.json"
    freeze.write_text(json.dumps(build_freeze_record(workspace)), encoding="utf-8")
    projection = tmp_path / "v4-projection.yaml"
    projection.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scenarios": [
                    {"scenario_id": "baseline", "probability": 0.6},
                    {"scenario_id": "downside", "probability": 0.4},
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorldModelValidationError, match="must match world-model"):
        validate_workspace(workspace, freeze_path=freeze, v4_projection=projection)


def test_validator_accepts_v2_forward_lineage(tmp_path: Path) -> None:
    inputs = _write_projection_inputs(
        tmp_path, documents=_workspace_documents(), projection=_v2_projection()
    )

    result = validate_workspace(
        inputs[0],
        freeze_path=inputs[1],
        v4_projection=inputs[2],
        v4_document=inputs[3],
        revision_diff=inputs[4],
    )

    assert result["material_claim_count"] == 1
    assert result["mapped_target_count"] == len(_v4_target_paths()) + len(_one_page_target_paths())
    assert result["lineage_enforced"] is True
    assert result["mapped_selected_evidence_count"] == 1
    assert result["true_orphan_node_count"] == 0
    assert result["true_orphan_edge_count"] == 0


def test_v4_target_manifest_covers_canonical_judgment_surface() -> None:
    document = _canonical_v4_document(_v4_document())

    paths, _ = _required_v4_paths(document)

    assert "/core/0/change_since_previous" in paths
    assert "/core/0/previous_scorecard_review" in paths
    assert "/core/9/monitoring_points/0/view_change" in paths
    assert "/core/0/fact_summary/0/summary" not in paths


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("stronger_output", "strengthens claim_strength"),
        ("stronger_source", "stronger than its weakest edge"),
        ("dropped_uncertainty", "drops uncertainty dimensions"),
        ("missing_source_uncertainty", "must carry all state uncertainty dimensions"),
        ("changed_basis", "changes a quantity basis"),
        ("unit_mismatch", "unit_transform"),
        ("invalid_source_unit", "units for series:a.statistic_value"),
        ("measure_unit_mismatch", "units for series:a.latest_value"),
        ("unavailable_snapshot_measure", "uses unavailable snapshot measure"),
        ("empty_edge_refs", "must contain at least 1 strings"),
        ("null_output_qualifier", "must be a mapping"),
        ("negative_probability", "probability is invalid"),
        ("off_grid_probability", "0.05 grid"),
        ("probability_sum", "must sum to 1.00"),
        ("missing_v4_target", "v4 judgment-bearing fields are unmapped"),
        ("missing_one_page_target", "one-page judgment-bearing fields are unmapped"),
        ("non_judgment_target", "is not judgment-bearing"),
        ("unmapped_selected", "selected evidence is absent from material claims"),
        ("disjoint_evidence", "evidence does not overlap state"),
        ("true_orphan", "graph contains true orphans"),
        ("unknown_projection_field", "v4-projection has unknown fields"),
        ("unknown_nested_field", "unknown fields"),
    ],
)
def test_validator_rejects_v2_lineage_bypasses(tmp_path: Path, case: str, expected: str) -> None:
    documents = copy.deepcopy(_workspace_documents())
    projection = _v2_projection()
    claim = projection["material_claims"][0]
    if case == "stronger_output":
        claim["targets"][0]["output_qualifier"]["claim_strength"] = "supported"
    elif case == "stronger_source":
        claim["source_qualifier"]["claim_strength"] = "identified"
    elif case == "dropped_uncertainty":
        claim["targets"][0]["output_qualifier"]["uncertainty"].remove("policy")
    elif case == "missing_source_uncertainty":
        claim["source_qualifier"]["uncertainty"].remove("policy")
        for target in claim["targets"]:
            target["output_qualifier"]["uncertainty"].remove("policy")
    elif case == "changed_basis":
        claim["targets"][0]["output_qualifier"]["basis"]["real_nominal"] = "real"
    elif case == "unit_mismatch":
        claim["targets"][0]["output_qualifier"]["units"] = ["basis-points"]
    elif case == "invalid_source_unit":
        claim["upstream"]["evidence"][0]["units"] = ["eur-million"]
        claim["source_qualifier"]["units"] = ["eur-million"]
    elif case == "measure_unit_mismatch":
        claim["upstream"]["evidence"][0]["measure"] = "latest_value"
    elif case == "unavailable_snapshot_measure":
        claim["upstream"]["evidence"][0]["measure"] = "unknown_measure"
    elif case == "empty_edge_refs":
        claim["upstream"]["edge_ids"] = []
    elif case == "null_output_qualifier":
        claim["targets"][0]["output_qualifier"] = None
    elif case == "negative_probability":
        projection["scenarios"][0]["probability"] = -0.1
    elif case == "off_grid_probability":
        projection["scenarios"][0]["probability"] = 0.61
    elif case == "probability_sum":
        projection["scenarios"][0]["probability"] = 0.4
    elif case == "missing_v4_target":
        claim["targets"] = [
            target
            for target in claim["targets"]
            if not (target["artifact"] == "v4" and target["field_path"] == "/summary")
        ]
    elif case == "missing_one_page_target":
        claim["targets"] = [
            target
            for target in claim["targets"]
            if not (
                target["artifact"] == "one_page"
                and target["field_path"] == "/key_judgments/j1/summary"
            )
        ]
    elif case == "non_judgment_target":
        claim["targets"].append(
            {
                "artifact": "v4",
                "field_path": "/as_of",
                "output_qualifier": _qualifier(),
            }
        )
    elif case == "unmapped_selected":
        snapshot = documents["evidence-snapshot.json"]
        extra_evidence = copy.deepcopy(snapshot["coverage_scan"][0])
        extra_evidence.update({"evidence_id": "series:b", "series_id": "b"})
        snapshot["coverage_scan"].append(extra_evidence)
        snapshot["coverage_manifest"].append(
            {"candidate_id": "series:b", "series_id": "b", "status": "selected"}
        )
        snapshot["canonical_payload_sha256"] = canonical_payload_sha256(snapshot)
        documents["evidence-packs.yaml"]["packs"][0]["evidence_for"].append("series:b")
    elif case == "disjoint_evidence":
        documents["evidence-packs.yaml"]["external_sources"].append(
            {
                "evidence_id": "ext:release",
                "source_tier": 1,
                "publisher": "Publisher",
                "title": "Release",
                "published_at": "2026-08-07",
                "url": "https://example.com/release",
            }
        )
        documents["evidence-packs.yaml"]["packs"][0]["evidence_for"].append("ext:release")
        claim["upstream"]["evidence"] = [
            {
                "evidence_id": "ext:release",
                "measure": "qualitative release statement",
                "units": ["not-applicable"],
            }
        ]
        claim["source_qualifier"]["units"] = ["not-applicable"]
        for target in claim["targets"]:
            target["output_qualifier"]["units"] = ["not-applicable"]
    elif case == "true_orphan":
        documents["world-model.yaml"]["graph"]["nodes"].append(
            {"node_id": "n3", "node_type": "latent_state", "label": "Unused"}
        )
    elif case == "unknown_projection_field":
        projection["unexpected"] = True
    else:
        claim["targets"][0]["unexpected"] = True
    inputs = _write_projection_inputs(tmp_path, documents=documents, projection=projection)

    with pytest.raises(WorldModelValidationError, match=expected):
        validate_workspace(
            inputs[0],
            freeze_path=inputs[1],
            v4_projection=inputs[2],
            v4_document=inputs[3],
            revision_diff=inputs[4],
        )


def test_validator_rejects_v4_probability_different_from_projection(tmp_path: Path) -> None:
    inputs = _write_projection_inputs(
        tmp_path, documents=_workspace_documents(), projection=_v2_projection()
    )
    v4_document = _v4_document()
    risk_section = next(
        section for section in v4_document["core"] if section["section_id"] == "risk_environment"
    )
    risk_section["scenarios"][0]["probability"] = 0.4
    risk_section["scenarios"][1]["probability"] = 0.4
    inputs[3].write_text(json.dumps(v4_document), encoding="utf-8")

    with pytest.raises(WorldModelValidationError, match="differs from v4-projection"):
        validate_workspace(
            inputs[0],
            freeze_path=inputs[1],
            v4_projection=inputs[2],
            v4_document=inputs[3],
            revision_diff=inputs[4],
        )


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("schema_version", None, "revision-diff.schema_version must be 1"),
        ("as_of", "2026-08-06", "revision-diff.as_of differs from cycle as_of"),
    ],
)
def test_validator_rejects_revision_diff_from_another_cycle(
    tmp_path: Path, field: str, value: object, expected: str
) -> None:
    inputs = _write_projection_inputs(
        tmp_path, documents=_workspace_documents(), projection=_v2_projection()
    )
    revision_diff = _revision_diff()
    if value is None:
        del revision_diff[field]
    else:
        revision_diff[field] = value
    inputs[4].write_text(json.dumps(revision_diff), encoding="utf-8")

    with pytest.raises(WorldModelValidationError, match=expected):
        validate_workspace(
            inputs[0],
            freeze_path=inputs[1],
            v4_projection=inputs[2],
            v4_document=inputs[3],
            revision_diff=inputs[4],
        )


def test_render_report_has_five_required_sections(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_workspace(workspace, _workspace_documents())
    freeze = tmp_path / "blind-freeze.json"
    freeze.write_text(json.dumps(build_freeze_record(workspace)), encoding="utf-8")
    diff = tmp_path / "revision-diff.yaml"
    diff.write_text(
        json.dumps(_revision_diff(changes=[{"area": "Growth", "summary": "Momentum softens."}])),
        encoding="utf-8",
    )

    report = render_report(workspace, freeze_path=freeze, revision_diff=diff)

    assert [line for line in report.splitlines() if line.startswith("## ")] == [
        "## Key judgments",
        "## Horizon paths",
        "## Unresolved tensions",
        "## Signposts",
        "## What changed",
    ]
    assert "Scenario 1 — Mechanism 1: demand path" in report
    assert "shock: Demand changes." in report
    assert "propagation: Credit transmits the change." in report
    assert "policy: Policy responds with a lag." in report


def test_render_plan_targets_empty_revision_fallback_and_checks_as_of(tmp_path: Path) -> None:
    documents = _workspace_documents()
    empty_diff = _revision_diff(changes=[])
    paths, _ = _required_one_page_paths(
        documents["world-model.yaml"],
        empty_diff,
        expected_as_of=date(2026, 8, 7),
    )
    assert "/revision_diff/no_evidence_backed_change" in paths

    workspace = tmp_path / "workspace"
    _write_workspace(workspace, documents)
    freeze = tmp_path / "blind-freeze.json"
    freeze.write_text(json.dumps(build_freeze_record(workspace)), encoding="utf-8")
    diff = tmp_path / "revision-diff.yaml"
    diff.write_text(json.dumps(empty_diff), encoding="utf-8")
    assert "No evidence-backed change is recorded." in render_report(
        workspace, freeze_path=freeze, revision_diff=diff
    )

    stale_diff = _revision_diff(changes=[])
    stale_diff["as_of"] = "2026-08-06"
    diff.write_text(json.dumps(stale_diff), encoding="utf-8")
    with pytest.raises(WorldModelValidationError, match="differs from cycle as_of"):
        render_report(workspace, freeze_path=freeze, revision_diff=diff)
