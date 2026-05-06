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
