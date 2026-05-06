from __future__ import annotations

import json
from pathlib import Path

from baibai_loop.validate.ledger import discover_ledger_files, validate_ledger_file


def _decision_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "decision_event_id": "decision-20260425-2767-research",
        "event_kind": "decision",
        "decision_scope": "research_memo",
        "ticker": "2767",
        "candidate_decision": "selected",
        "research_decision": {"outcome": "approved", "posture": "act_now"},
        "trade_execution_state": "none",
    }
    record.update(overrides)
    return record


def _write_jsonl(path: Path, record: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")


def test_discover_ledger_files_finds_research_decision_register(tmp_path: Path) -> None:
    path = tmp_path / "records/_ledger" / "research-decisions" / "2026-04.jsonl"
    _write_jsonl(path, _decision_record())
    assert discover_ledger_files(tmp_path / "records/_ledger") == [path]


def test_validate_ledger_invalid_json_is_finding(tmp_path: Path) -> None:
    path = tmp_path / "records/_ledger" / "research-decisions" / "2026-04.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("{bad\n", encoding="utf-8")
    assert "ledger.invalid-json" in {finding.code for finding in validate_ledger_file(path)}


def test_validate_ledger_non_object_line_is_finding(tmp_path: Path) -> None:
    path = tmp_path / "records/_ledger" / "research-decisions" / "2026-04.jsonl"
    _write_jsonl(path, ["not", "object"])
    assert "ledger.non-object" in {finding.code for finding in validate_ledger_file(path)}


def test_validate_ledger_missing_required_field_is_finding(tmp_path: Path) -> None:
    path = tmp_path / "records/_ledger" / "research-decisions" / "2026-04.jsonl"
    record = _decision_record()
    del record["ticker"]
    _write_jsonl(path, record)
    assert "ledger.required" in {finding.code for finding in validate_ledger_file(path)}


def test_validate_ledger_rejects_bad_decision_scope(tmp_path: Path) -> None:
    path = tmp_path / "records/_ledger" / "research-decisions" / "2026-04.jsonl"
    _write_jsonl(path, _decision_record(decision_scope="trade"))
    assert "ledger.enum" in {finding.code for finding in validate_ledger_file(path)}


def test_validate_ledger_rejects_bad_ticker_pattern(tmp_path: Path) -> None:
    path = tmp_path / "records/_ledger" / "research-decisions" / "2026-04.jsonl"
    _write_jsonl(path, _decision_record(ticker="bad"))
    assert "ledger.pattern" in {finding.code for finding in validate_ledger_file(path)}


def test_candidate_coverage_requires_decision_event_for_hit_candidate(tmp_path: Path) -> None:
    candidates_path = tmp_path / "records/04-candidates/2026/05/2026-05-01.yaml"
    candidates_path.parent.mkdir(parents=True)
    candidates_path.write_text(
        "requires_decision_coverage: true\n"
        "candidates:\n"
        "- ticker: '9682'\n"
        "  candidate_id: candidate-2026-05-01-9682\n"
        "  playbook_screen_result: hit\n"
        "  policy_gate_result: pass\n"
        "  liquidity_gate_result: pass\n"
        "  macro_regime_gate_result: pass\n",
        encoding="utf-8",
    )
    path = tmp_path / "records/_ledger" / "research-decisions" / "2026-05.jsonl"
    _write_jsonl(path, _decision_record(ticker="2767"))

    codes = {finding.code for finding in validate_ledger_file(path)}

    assert "ledger.candidate-decision-coverage" in codes


def test_candidate_coverage_allows_pre_migration_exception(tmp_path: Path) -> None:
    candidates_path = tmp_path / "records/04-candidates/2026/05/2026-05-01.yaml"
    candidates_path.parent.mkdir(parents=True)
    candidates_path.write_text(
        "requires_decision_coverage: false\n"
        "decision_coverage_exception: pre_migration_unregistered_population\n"
        "candidates:\n"
        "- ticker: '9682'\n"
        "  candidate_id: candidate-2026-05-01-9682\n"
        "  playbook_screen_result: hit\n"
        "  policy_gate_result: pass\n"
        "  liquidity_gate_result: pass\n"
        "  macro_regime_gate_result: pass\n",
        encoding="utf-8",
    )
    path = tmp_path / "records/_ledger" / "research-decisions" / "2026-05.jsonl"
    _write_jsonl(path, _decision_record(ticker="2767"))

    codes = {finding.code for finding in validate_ledger_file(path)}

    assert "ledger.candidate-decision-coverage" not in codes
    assert "ledger.candidate-coverage-summary" in codes


def test_pre_migration_exception_summary_must_match_reviewed_population(tmp_path: Path) -> None:
    candidate_ref = "records/04-candidates/2026/05/2026-05-01.yaml"
    candidates_path = tmp_path / candidate_ref
    candidates_path.parent.mkdir(parents=True)
    candidates_path.write_text(
        "requires_decision_coverage: false\n"
        "decision_coverage_exception: pre_migration_unregistered_population\n"
        "candidates:\n"
        "- ticker: '9682'\n"
        "  candidate_id: candidate-2026-05-01-9682\n"
        "  playbook_screen_result: hit\n"
        "  policy_gate_result: pass\n"
        "  liquidity_gate_result: pass\n"
        "  macro_regime_gate_result: pass\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "records/_migrations/manifest.jsonl"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "migration_run_id": "domain-model-103-20260506",
                "migration_strategy": "summarize",
                "granularity": "repository",
                "unregistered_candidate_population": {
                    "candidate_run": candidate_ref,
                    "screened_population": 1,
                    "hit_or_near_threshold_population": 1,
                    "reviewed_population": 0,
                    "auto_backfilled_not_reviewed_rows": 0,
                    "pre_migration_unresearched_candidate_backfill": False,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    path = tmp_path / "records/_ledger" / "research-decisions" / "2026-05.jsonl"
    _write_jsonl(
        path,
        _decision_record(
            ticker="9682",
            candidate_ref={
                "candidates_ref": candidate_ref,
                "candidate_id": "candidate-2026-05-01-9682",
                "ticker": "9682",
            },
        ),
    )

    codes = {finding.code for finding in validate_ledger_file(path)}

    assert "ledger.candidate-coverage-summary" in codes
