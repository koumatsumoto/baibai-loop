from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import yaml
from tools.migrations.cutover_application_v18 import CutoverError, cutover

from baibai_engine.appdb.schema import SCHEMA_SQL
from baibai_engine.research.assessment import (
    BargainAssessment,
    derive_case_machine_values,
)
from baibai_engine.research.thesis import ThesisDocument

ROOT = Path(__file__).resolve().parents[2]


def _source_schema() -> str:
    return SCHEMA_SQL.replace(
        "session_kind IN ('opportunity', 'earnings-material-event')",
        "session_kind IN ("
        "'opportunity', 'pending-result', 'monthly-contribution', "
        "'earnings-material-event', 'annual-outcome'"
        ")",
    )


def _seed_source(path: Path, *, machine_drift: bool = False, active_retired: bool = False) -> None:
    thesis = yaml.safe_load(
        (ROOT / "tests/fixtures/thesis/2331-decision.yaml").read_text(encoding="utf-8")
    )
    machine = derive_case_machine_values(ThesisDocument.model_validate(thesis))
    if machine_drift:
        machine["fair_value_yen"] = 999999.0
    shortlist = {
        "schema_version": 6,
        "kind": "shortlist",
        "shortlist_id": "shortlist-20260830-cutover",
        "selection_id": "selection-cutover",
        "run_revision_id": "run-cutover",
        "as_of": "2026-08-30",
        "published_at": "2026-08-30T10:00:00+09:00",
        "macro_context_id": None,
        "review_basis_shortlist_id": None,
        "research_gate_contract_id": "research-gate-v1",
        "entries": [
            {
                "ticker": "2331",
                "decision": "selected",
                "reason": "一次研究で仮説を識別する",
                "reject_class": None,
                "rank": 1,
                "narrative": {
                    "ploss": "中低",
                    "why": "仮説",
                    "temporary": "一時要因",
                    "structural": "構造要因",
                    "survive": "存続性",
                    "unlock": "価値実現",
                    "counter": "反証",
                    "research": "確認事項",
                    "value": "追加価値",
                    "prov": "暫定判断",
                    "upside": "上値",
                    "downside": "下値",
                    "rr": "非対称性",
                    "catalyst": "次回決算",
                    "catalyst_date": None,
                    "macro": "macro影響",
                    "sector_label": None,
                },
                "er_annual": 0.1,
                "machine_snapshot": None,
            }
        ],
    }
    assessment = {
        "schema_version": 4,
        "kind": "bargain_assessment",
        "assessment_id": "bargain-assessment-20260830-cutover",
        "as_of": "2026-08-30",
        "published_at": "2026-08-30T12:00:00+09:00",
        "result": "no_actionable_bargain",
        "headline": "現時点では見送る",
        "shortlist_id": shortlist["shortlist_id"],
        "macro_context_id": None,
        "comparison": "要求利回りを満たさない",
        "forgone": "次回決算で再評価する",
        "cases": [
            {
                "ticker": "2331",
                "name": "テスト銘柄",
                "disposition": "reject",
                "disposition_reason": "要求利回りを満たさない",
                "reject_class": "price_already_converged",
                "thesis_id": "thesis-cutover",
                "thesis_core_sha256": "a" * 64,
                "review_id": None,
                "machine": machine,
                "business_model": "継続課金",
                "value_capture": "価格決定力",
                "growth_quality": "再投資可能",
                "financial_resilience": "無借金",
                "strongest_countercase": "成長鈍化",
                "catalyst": "次回決算",
                "research_questions": [
                    {"question": "粗利率", "answer": "維持", "status": "answered"}
                ],
                "unknowns": [],
                "source_caveats": [],
            }
        ],
        "review": {
            "attempt": 1,
            "reviewer_identity": "independent-reviewer",
            "reviewed_at": "2026-08-30T11:00:00+09:00",
            "conclusion": "pass",
            "draft_sha256": "b" * 64,
            "open_findings": [],
        },
    }
    operation_payload = json.dumps(
        {
            "checkpoint": "final",
            "artifacts": [],
            "canonical_refs": ["ledger_event: event-release"],
            "human_confirmation": {"request": "注文結果", "result": "失効"},
            "completion_reason": None,
            "result": "recorded",
            "next": "wait",
        }
    )
    with sqlite3.connect(path) as connection:
        connection.executescript(_source_schema())
        connection.execute(
            "INSERT INTO shortlist VALUES (?, ?, ?, ?, ?, ?)",
            (
                shortlist["shortlist_id"],
                shortlist["selection_id"],
                shortlist["run_revision_id"],
                shortlist["as_of"],
                shortlist["published_at"],
                json.dumps(shortlist, ensure_ascii=False),
            ),
        )
        connection.execute(
            "INSERT INTO thesis VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "thesis-cutover",
                "2331",
                thesis["input_snapshot"]["as_of"],
                thesis["judgment"]["recommendation"],
                thesis["judgment"]["proposed_at"],
                None,
                json.dumps(thesis, ensure_ascii=False),
                "a" * 64,
            ),
        )
        connection.execute(
            "INSERT INTO bargain_assessment VALUES (?, ?, ?, ?, ?, ?)",
            (
                assessment["assessment_id"],
                assessment["as_of"],
                assessment["published_at"],
                assessment["result"],
                assessment["shortlist_id"],
                json.dumps(assessment, ensure_ascii=False),
            ),
        )
        connection.execute(
            "INSERT INTO ledger_event VALUES (1, 'event-release', "
            "'2026-08-30T12:30:00+09:00', 0, 'release', NULL, NULL, '{}')"
        )
        connection.execute(
            "INSERT INTO operation_session VALUES (?, 'pending-result', ?, '2026-08-30', "
            "NULL, '2026-08-30T12:00:00+09:00', ?, ?)",
            (
                "op-20260830-pending-result-1",
                "active" if active_retired else "completed",
                None if active_retired else "2026-08-30T13:00:00+09:00",
                operation_payload,
            ),
        )
        connection.execute("PRAGMA user_version = 17")
        connection.commit()


def test_cutover_removes_only_duplicate_fields_and_resolved_retired_operations(
    tmp_path: Path,
) -> None:
    source = tmp_path / "v17.sqlite"
    output = tmp_path / "v18.sqlite"
    _seed_source(source)

    report = cutover(source, output)

    assert report["schema_version"] == 18
    assert report["assessment_machine_cases_compared"] == 1
    assert report["dropped_operations"] == [
        {
            "operation_id": "op-20260830-pending-result-1",
            "session_kind": "pending-result",
            "canonical_refs": ["ledger_event: event-release"],
        }
    ]
    with sqlite3.connect(output) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA user_version").fetchone() == (18,)
        shortlist = json.loads(connection.execute("SELECT payload FROM shortlist").fetchone()[0])
        assessment_raw = json.loads(
            connection.execute("SELECT payload FROM bargain_assessment").fetchone()[0]
        )
        assert shortlist["schema_version"] == 7
        assert "reject_class" not in shortlist["entries"][0]
        assert assessment_raw["schema_version"] == 5
        assert "reject_class" not in assessment_raw["cases"][0]
        assert "machine" not in assessment_raw["cases"][0]
        assessment = BargainAssessment.model_validate(assessment_raw)
        assert assessment.review.draft_sha256 != "b" * 64
        assert connection.execute("SELECT count(*) FROM operation_session").fetchone() == (0,)
        assert connection.execute(
            "SELECT event_id, event_type, payload FROM ledger_event"
        ).fetchone() == ("event-release", "release", "{}")


def test_cutover_rejects_a_retired_active_operation(tmp_path: Path) -> None:
    source = tmp_path / "v17.sqlite"
    _seed_source(source, active_retired=True)

    with pytest.raises(CutoverError, match="still active"):
        cutover(source, tmp_path / "v18.sqlite")


def test_cutover_rejects_an_assessment_machine_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "v17.sqlite"
    _seed_source(source, machine_drift=True)

    with pytest.raises(CutoverError, match="machine copy differs"):
        cutover(source, tmp_path / "v18.sqlite")
