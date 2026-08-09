from __future__ import annotations

import copy
import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from tools.experiments.macro_world_model.build_evidence_snapshot import (
    build_snapshot,
    canonical_payload_sha256,
)
from tools.experiments.macro_world_model.render_report import render_report
from tools.experiments.macro_world_model.validate_world_model import (
    WorldModelValidationError,
    _reject_prior_material,
    build_freeze_record,
    validate_workspace,
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
    )

    expected = first["canonical_payload_sha256"]
    assert expected == second["canonical_payload_sha256"]
    assert expected == canonical_payload_sha256(first)
    assert expected == canonical_payload_sha256(second)


def test_snapshot_detects_same_observed_at_vintages_as_revision(tmp_path: Path) -> None:
    store = tmp_path / "macro.sqlite"
    _macro_store(store)
    snapshot = build_snapshot(reading=_reading(), macro_db=store)

    scan = cast(list[dict[str, Any]], snapshot["coverage_scan"])
    series_a = next(row for row in scan if row["series_id"] == "a")
    assert series_a["revisions"] == [
        {
            "observed_at": "2026-08-01",
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
    assert "recent_revision" in series_a["machine_materiality_reasons"]


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
            "coverage_scan": [{"evidence_id": evidence_id, "series_id": "a"}],
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
                }
                for rank, scenario_id in enumerate(("baseline", "downside"), start=1)
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
        ("uncovered_selected", "absent from evidence packs"),
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
    elif case == "uncovered_selected":
        snapshot = documents["evidence-snapshot.json"]
        snapshot["coverage_scan"].append({"evidence_id": "series:b", "series_id": "b"})
        snapshot["coverage_manifest"].append(
            {"candidate_id": "series:b", "series_id": "b", "status": "selected"}
        )
        snapshot["canonical_payload_sha256"] = canonical_payload_sha256(snapshot)
    else:
        documents["charter.yaml"]["conditioning_assumptions"] = ["macro-context-2026-07-31-prior"]
    workspace = tmp_path / case
    _write_workspace(workspace, documents)

    with pytest.raises(WorldModelValidationError, match=expected):
        validate_workspace(workspace)


def test_validator_rejects_freeze_drift_and_probability_order(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    documents = _workspace_documents()
    _write_workspace(workspace, documents)
    freeze = tmp_path / "blind-freeze.json"
    freeze.write_text(json.dumps(build_freeze_record(workspace)), encoding="utf-8")
    projection = tmp_path / "v4-projection.yaml"
    projection.write_text(
        json.dumps(
            {
                "scenarios": [
                    {"scenario_id": "baseline", "probability": 0.4},
                    {"scenario_id": "downside", "probability": 0.6},
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorldModelValidationError, match="contradicts"):
        validate_workspace(workspace, freeze_path=freeze, v4_projection=projection)

    drifted_states = copy.deepcopy(documents["states.yaml"])
    drifted_states["states"][0]["uncertainty"]["data"] = "low"
    (workspace / "states.yaml").write_text(
        json.dumps(drifted_states, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(WorldModelValidationError, match="digests do not match"):
        validate_workspace(workspace, freeze_path=freeze)


def test_render_report_has_five_required_sections(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_workspace(workspace, _workspace_documents())
    freeze = tmp_path / "blind-freeze.json"
    freeze.write_text(json.dumps(build_freeze_record(workspace)), encoding="utf-8")
    diff = tmp_path / "revision-diff.yaml"
    diff.write_text(
        json.dumps({"changes": [{"area": "Growth", "summary": "Momentum softens."}]}),
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
