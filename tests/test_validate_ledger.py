from __future__ import annotations

import json
from pathlib import Path

from baibai_loop.validate.ledger import discover_ledger_files, validate_ledger_file


def _paper_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "ledger_id": "paper-20260425-2767-vmean",
        "ticker": "2767",
        "name": "Sample",
        "decision": "accepted",
        "playbook": "valuation-mean-reversion-v1",
        "screened_ref": "screened/2026/04/2026-04-24.yaml",
        "research_ref": "research/2026/04/sample.md",
        "asof_date": "2026-04-24",
        "decision_date": "2026-04-25",
        "baseline_price": 100.0,
        "market_cap_oku": 500.0,
        "avg_turnover_oku": 10.0,
        "threshold_hit_count": 2,
        "macro_gate": "neutral",
        "adv_participation_pct": 0.1,
        "adjustment_applied": False,
        "tracking": {"plus_15bd": None, "plus_30bd": None},
    }
    record.update(overrides)
    return record


def _write_jsonl(path: Path, record: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")


def test_discover_ledger_files_finds_paper_and_skipped(tmp_path: Path) -> None:
    paper = tmp_path / "ledger" / "paper" / "2026-04.jsonl"
    skipped = tmp_path / "ledger" / "skipped" / "2026-04.jsonl"
    _write_jsonl(paper, _paper_record())
    _write_jsonl(
        skipped,
        {
            **_paper_record(),
            "ledger_id": "skipped-20260425-2767-vmean",
            "decision": "skipped",
            "research_ref": None,
        },
    )
    assert discover_ledger_files(tmp_path / "ledger") == [paper, skipped]


def test_validate_ledger_invalid_json_is_finding(tmp_path: Path) -> None:
    path = tmp_path / "ledger" / "paper" / "2026-04.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("{bad\n", encoding="utf-8")
    assert "ledger.invalid-json" in {finding.code for finding in validate_ledger_file(path)}


def test_validate_ledger_non_object_line_is_finding(tmp_path: Path) -> None:
    path = tmp_path / "ledger" / "paper" / "2026-04.jsonl"
    _write_jsonl(path, ["not", "object"])
    assert "ledger.non-object" in {finding.code for finding in validate_ledger_file(path)}


def test_validate_ledger_missing_required_field_is_finding(tmp_path: Path) -> None:
    path = tmp_path / "ledger" / "paper" / "2026-04.jsonl"
    record = _paper_record()
    del record["ticker"]
    _write_jsonl(path, record)
    assert "ledger.required" in {finding.code for finding in validate_ledger_file(path)}


def test_validate_ledger_rejects_adv_participation_cap(tmp_path: Path) -> None:
    path = tmp_path / "ledger" / "paper" / "2026-04.jsonl"
    _write_jsonl(path, _paper_record(adv_participation_pct=5.0))
    assert "ledger.exclusiveMaximum" in {finding.code for finding in validate_ledger_file(path)}


def test_validate_ledger_rejects_bad_ledger_id_pattern(tmp_path: Path) -> None:
    path = tmp_path / "ledger" / "paper" / "2026-04.jsonl"
    _write_jsonl(path, _paper_record(ledger_id="bad"))
    assert "ledger.pattern" in {finding.code for finding in validate_ledger_file(path)}
