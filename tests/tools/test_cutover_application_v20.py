from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from tools.migrations.cutover_application_v20 import CutoverError, cutover

from baibai_engine.appdb.schema import SCHEMA_SQL
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.research.thesis import ThesisDocument

ROOT = Path(__file__).resolve().parents[2]


def _legacy_thesis() -> dict[str, object]:
    raw = safe_load((ROOT / "tests/fixtures/thesis/2331-decision.yaml").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    raw["schema_version"] = 2
    judgment = raw["judgment"]
    assert isinstance(judgment, dict)
    judgment["ai_value_capture"] = {
        "assessment_status": "material",
        "roles": ["adopter"],
        "competitive_advantage": "favorable",
        "pricing_power": "favorable",
        "capex_burden": "neutral",
        "customer_bargaining_power": "neutral",
        "value_capture_conclusion": "captured",
        "decision_weight": "supporting",
        "rationale": "Automation can improve recurring service capacity.",
        "source_ids": ["primary-results"],
    }
    return raw


def _seed_source(
    path: Path,
    *,
    version: int = 19,
    missing_core: bool = False,
    missing_ai: bool = False,
) -> None:
    thesis = _legacy_thesis()
    if missing_ai:
        judgment = thesis["judgment"]
        assert isinstance(judgment, dict)
        judgment.pop("ai_value_capture")
    review = safe_load(
        (ROOT / "tests/fixtures/thesis/2331-decision-review.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(review, dict)
    recorded_hash = "a" * 64
    review["reviewed_thesis_sha256"] = recorded_hash
    assessment = {
        "schema_version": 1,
        "kind": "capital_allocation_assessment",
        "capital_allocation_assessment_id": "capital-allocation-assessment-20260703-cutover",
        "as_of": "2026-07-03",
        "published_at": "2026-07-03T11:00:00+09:00",
        "result": "allocate",
        "headline": "Allocate to the reviewed alternative.",
        "research_triage_id": "research-triage-cutover",
        "macro_context_id": "macro-cutover",
        "comparison": "The selected alternative has the strongest risk-adjusted value.",
        "forgone": "Cash remains the alternative.",
        "alternatives": [
            {
                "ticker": "2331",
                "thesis_id": "thesis-cutover",
                "thesis_core_sha256": recorded_hash,
                "thesis_review_id": "review-cutover",
                "disposition": "allocate",
                "rationale": "The required return is available.",
            }
        ],
        "review": {
            "attempt": 1,
            "reviewer_identity": "independent-reviewer",
            "reviewed_at": "2026-07-03T10:30:00+09:00",
            "conclusion": "pass",
            "draft_sha256": "b" * 64,
            "open_findings": [],
        },
    }
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA_SQL)
        connection.execute(
            "INSERT INTO task VALUES (?, 'open', 'ops', NULL, ?, NULL, ?, NULL, ?)",
            ("task-cutover", "2026-09-02", "2026-09-02T09:00:00+09:00", "{}"),
        )
        connection.execute(
            "INSERT INTO macro_context VALUES (?, 5, ?, ?, NULL, ?)",
            ("macro-cutover", "2026-07-03", "2026-07-03T08:00:00+09:00", "{}"),
        )
        connection.execute("INSERT INTO macro_context_head VALUES (1, 'macro-cutover')")
        connection.execute(
            "INSERT INTO research_triage VALUES (?, ?, ?, ?, ?, ?)",
            (
                "research-triage-cutover",
                "review-set-cutover",
                "run-cutover",
                "2026-07-03",
                "2026-07-03T08:30:00+09:00",
                "{}",
            ),
        )
        connection.execute(
            "INSERT INTO thesis VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                "thesis-cutover",
                "2331",
                "2026-07-03",
                "buy",
                "2026-07-03T10:00:00+09:00",
                json.dumps(thesis, ensure_ascii=False),
                None if missing_core else recorded_hash,
            ),
        )
        connection.execute(
            "INSERT INTO thesis_review VALUES (?, ?, ?, ?)",
            (
                "review-cutover",
                "thesis-cutover",
                "2026-07-03T10:30:00+09:00",
                json.dumps(review, ensure_ascii=False),
            ),
        )
        connection.execute(
            "INSERT INTO position_review VALUES (?, ?, ?, ?, NULL, ?)",
            ("position-review-cutover", "2331", "2026-07-03", "thesis-cutover", "{}"),
        )
        connection.execute(
            "INSERT INTO capital_allocation_assessment VALUES (?, ?, ?, 'allocate', ?, ?)",
            (
                assessment["capital_allocation_assessment_id"],
                assessment["as_of"],
                assessment["published_at"],
                assessment["research_triage_id"],
                json.dumps(assessment, ensure_ascii=False),
            ),
        )
        connection.execute(
            "INSERT INTO operation_session VALUES (?, 'capital-allocation', 'completed', ?, "
            "NULL, ?, ?, ?)",
            (
                "operation-cutover",
                "2026-07-03",
                "2026-07-03T08:30:00+09:00",
                "2026-07-03T11:30:00+09:00",
                "{}",
            ),
        )
        connection.execute(
            "INSERT INTO ledger_event VALUES (1, ?, ?, 0, 'opening_balance', NULL, NULL, ?)",
            ("event-cutover", "2026-07-03T12:00:00+09:00", "{}"),
        )
        connection.execute(
            "INSERT INTO ledger_market_price VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "2331",
                "2026-07-03T15:30:00+09:00",
                "1000",
                "licensed_dataset",
                "unadjusted_close",
                "source-cutover",
                "{}",
            ),
        )
        connection.execute(
            "INSERT INTO ledger_meta VALUES (1, 2, 'repository_only', ?, NULL, NULL, ?)",
            ("2026-07-03", "{}"),
        )
        connection.execute(
            "INSERT INTO portfolio_outcome VALUES (?, '1y', ?, ?, 'unresolved', ?)",
            ("outcome-cutover", "2025-07-03", "2026-07-03", "{}"),
        )
        connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()


def test_cutover_changes_only_thesis_schema_and_retired_field(tmp_path: Path) -> None:
    source = tmp_path / "v19.sqlite"
    output = tmp_path / "v20.sqlite"
    _seed_source(source)

    report = cutover(source, output)

    assert report["schema_version"] == 20
    assert report["transformed_theses"] == 1
    with sqlite3.connect(source) as before, sqlite3.connect(output) as after:
        assert after.execute("PRAGMA user_version").fetchone() == (20,)
        assert after.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert after.execute("PRAGMA foreign_key_check").fetchall() == []
        before_row = before.execute("SELECT * FROM thesis").fetchone()
        after_row = after.execute("SELECT * FROM thesis").fetchone()
        assert before_row is not None
        assert after_row is not None
        assert before_row[:6] == after_row[:6]
        assert before_row[7] == after_row[7] == "a" * 64
        before_payload = json.loads(before_row[6])
        after_payload = json.loads(after_row[6])
        assert isinstance(before_payload["judgment"], dict)
        before_payload["schema_version"] = 3
        before_payload["judgment"].pop("ai_value_capture")
        assert after_payload == before_payload
        ThesisDocument.model_validate(after_payload)
        review_payload = after.execute("SELECT payload FROM thesis_review").fetchone()[0]
        assessment_payload = after.execute(
            "SELECT payload FROM capital_allocation_assessment"
        ).fetchone()[0]
        assert review_payload == before.execute("SELECT payload FROM thesis_review").fetchone()[0]
        assert (
            assessment_payload
            == before.execute("SELECT payload FROM capital_allocation_assessment").fetchone()[0]
        )
        assert json.loads(review_payload)["reviewed_thesis_sha256"] == "a" * 64
        assert json.loads(assessment_payload)["alternatives"][0]["thesis_core_sha256"] == "a" * 64


def test_cutover_rejects_wrong_source_version_without_output(tmp_path: Path) -> None:
    source = tmp_path / "v18.sqlite"
    output = tmp_path / "v20.sqlite"
    _seed_source(source, version=18)

    with pytest.raises(CutoverError, match="expected 19"):
        cutover(source, output)

    assert not output.exists()


def test_cutover_rejects_existing_output_without_changing_it(tmp_path: Path) -> None:
    source = tmp_path / "v19.sqlite"
    output = tmp_path / "v20.sqlite"
    _seed_source(source)
    output.write_bytes(b"existing")

    with pytest.raises(CutoverError, match="output already exists"):
        cutover(source, output)

    assert output.read_bytes() == b"existing"


def test_cutover_rejects_missing_recorded_identity_without_output(tmp_path: Path) -> None:
    source = tmp_path / "v19.sqlite"
    output = tmp_path / "v20.sqlite"
    _seed_source(source, missing_core=True)

    with pytest.raises(CutoverError, match="no recorded core_sha256"):
        cutover(source, output)

    assert not output.exists()


def test_cutover_rejects_missing_retired_field_without_output(tmp_path: Path) -> None:
    source = tmp_path / "v19.sqlite"
    output = tmp_path / "v20.sqlite"
    _seed_source(source, missing_ai=True)

    with pytest.raises(CutoverError, match="not a current v2 payload"):
        cutover(source, output)

    assert not output.exists()
