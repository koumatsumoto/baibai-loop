from __future__ import annotations

import json
from pathlib import Path

from baibai_loop.validation.decisions import discover_decisions_files, validate_decisions_file


def _decision_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "decision_event_id": "decision-20260425-2767-research",
        "event_kind": "decision",
        "decision_scope": "research_memo",
        "ticker": "2767",
        "thesis_decision": {"outcome": "approved", "posture": "act_now"},
        "tracking": {"mode": "post_approval"},
        "execution_state": "none",
    }
    record.update(overrides)
    return record


def _write_jsonl(path: Path, *records: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def test_discover_decisions_files_finds_thesis_decision_register(tmp_path: Path) -> None:
    path = tmp_path / "records/_decisions" / "thesis-decisions" / "2026-04.jsonl"
    _write_jsonl(path, _decision_record())
    assert discover_decisions_files(tmp_path / "records/_decisions") == [path]


def test_validate_decisions_invalid_json_is_finding(tmp_path: Path) -> None:
    path = tmp_path / "records/_decisions" / "thesis-decisions" / "2026-04.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("{bad\n", encoding="utf-8")
    assert "decisions.invalid-json" in {finding.code for finding in validate_decisions_file(path)}


def test_validate_decisions_non_object_line_is_finding(tmp_path: Path) -> None:
    path = tmp_path / "records/_decisions" / "thesis-decisions" / "2026-04.jsonl"
    _write_jsonl(path, ["not", "object"])
    assert "decisions.non-object" in {finding.code for finding in validate_decisions_file(path)}


def test_validate_decisions_missing_required_field_is_finding(tmp_path: Path) -> None:
    path = tmp_path / "records/_decisions" / "thesis-decisions" / "2026-04.jsonl"
    record = _decision_record()
    del record["ticker"]
    _write_jsonl(path, record)
    assert "decisions.required" in {finding.code for finding in validate_decisions_file(path)}


def test_validate_decisions_rejects_bad_decision_scope(tmp_path: Path) -> None:
    path = tmp_path / "records/_decisions" / "thesis-decisions" / "2026-04.jsonl"
    _write_jsonl(path, _decision_record(decision_scope="trade"))
    assert "decisions.enum" in {finding.code for finding in validate_decisions_file(path)}


def test_validate_decisions_rejects_bad_ticker_pattern(tmp_path: Path) -> None:
    path = tmp_path / "records/_decisions" / "thesis-decisions" / "2026-04.jsonl"
    _write_jsonl(path, _decision_record(ticker="bad"))
    assert "decisions.pattern" in {finding.code for finding in validate_decisions_file(path)}
