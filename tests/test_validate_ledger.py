from __future__ import annotations

import json
from pathlib import Path

from baibai_loop.validate.ledger import discover_ledger_files, validate_ledger_file


def _decision_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "decision_event_id": "decision-20260425-2767-research",
        "event_kind": "decision",
        "decision_scope": "research_memo",
        "provenance": "regenerated",
        "ticker": "2767",
        "research_decision": {"outcome": "approved", "posture": "act_now"},
        "tracking": {"mode": "post_approval"},
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


def test_validate_ledger_rejects_removed_reference_and_hash_fields(tmp_path: Path) -> None:
    path = tmp_path / "records/_ledger" / "research-decisions" / "2026-04.jsonl"
    record = _decision_record(
        **{"_".join(("policy", "snapshot")): {"ref_path": "records/01-policy/2026/05/policy.md"}},
        row_sha256="sha256:bad",
    )
    _write_jsonl(path, record)

    codes = {finding.code for finding in validate_ledger_file(path)}

    assert "ledger.removed-reference-field" in codes
    assert "ledger.removed-hash-field" in codes


def test_validate_ledger_rejects_removed_not_reviewed_reason(tmp_path: Path) -> None:
    path = tmp_path / "records/_ledger" / "research-decisions" / "2026-04.jsonl"
    record = _decision_record(**{"_".join(("not", "reviewed", "reason")): "rank_out"})
    _write_jsonl(path, record)

    codes = {finding.code for finding in validate_ledger_file(path)}

    assert "ledger.removed-reference-field" in codes


def test_validate_ledger_rejects_removed_candidate_decision(tmp_path: Path) -> None:
    path = tmp_path / "records/_ledger" / "research-decisions" / "2026-04.jsonl"
    record = _decision_record(**{"_".join(("candidate", "decision")): "selected"})
    _write_jsonl(path, record)

    codes = {finding.code for finding in validate_ledger_file(path)}

    assert "ledger.removed-reference-field" in codes


def test_validate_ledger_rejects_bad_decision_scope(tmp_path: Path) -> None:
    path = tmp_path / "records/_ledger" / "research-decisions" / "2026-04.jsonl"
    _write_jsonl(path, _decision_record(decision_scope="trade"))
    assert "ledger.enum" in {finding.code for finding in validate_ledger_file(path)}


def test_validate_ledger_rejects_bad_ticker_pattern(tmp_path: Path) -> None:
    path = tmp_path / "records/_ledger" / "research-decisions" / "2026-04.jsonl"
    _write_jsonl(path, _decision_record(ticker="bad"))
    assert "ledger.pattern" in {finding.code for finding in validate_ledger_file(path)}


def test_research_memo_candidate_ref_rejects_mismatched_screen_run_id(tmp_path: Path) -> None:
    candidate_ref = "records/04-candidates/2026/05/2026-05-01.yaml"
    candidates_path = tmp_path / candidate_ref
    candidates_path.parent.mkdir(parents=True)
    candidates_path.write_text(
        "run_id: screening-20260501\n"
        "candidates:\n"
        "- ticker: '9682'\n"
        "  candidate_id: candidate-2026-05-01-9682\n"
        "  screen_run_id: screening-20260501\n"
        "  playbook_screen_result: hit\n"
        "  policy_gate_result: pass\n"
        "  liquidity_gate_result: pass\n"
        "  macro_regime_gate_result: pass\n",
        encoding="utf-8",
    )
    path = tmp_path / "records/_ledger" / "research-decisions" / "2026-05.jsonl"
    _write_jsonl(
        path,
        _decision_record(
            decision_event_id="decision-20260501-9682-research",
            decision_scope="research_memo",
            ticker="9682",
            candidate_ref={
                "candidates_ref": candidate_ref,
                "candidate_id": "candidate-2026-05-01-9682",
                "screen_run_id": "screening-20260508",
                "ticker": "9682",
            },
        ),
    )

    codes = {finding.code for finding in validate_ledger_file(path)}

    assert "ledger.candidate-ref-screen-run-id" in codes


def test_candidate_ref_requires_screen_run_id(tmp_path: Path) -> None:
    candidate_ref = "records/04-candidates/2026/05/2026-05-01.yaml"
    candidates_path = tmp_path / candidate_ref
    candidates_path.parent.mkdir(parents=True)
    candidates_path.write_text(
        "run_id: screening-20260501\n"
        "candidates:\n"
        "- ticker: '9682'\n"
        "  candidate_id: candidate-2026-05-01-9682\n"
        "  screen_run_id: screening-20260501\n"
        "  playbook_screen_result: hit\n"
        "  policy_gate_result: pass\n"
        "  liquidity_gate_result: pass\n"
        "  macro_regime_gate_result: pass\n",
        encoding="utf-8",
    )
    path = tmp_path / "records/_ledger" / "research-decisions" / "2026-05.jsonl"
    _write_jsonl(
        path,
        _decision_record(
            decision_event_id="decision-20260501-9682-research",
            decision_scope="research_memo",
            ticker="9682",
            candidate_ref={
                "candidates_ref": candidate_ref,
                "candidate_id": "candidate-2026-05-01-9682",
                "ticker": "9682",
            },
        ),
    )

    codes = {finding.code for finding in validate_ledger_file(path)}

    assert "ledger.candidate-ref-screen-run-id" in codes


def test_candidate_ref_missing_candidates_ref_is_error(tmp_path: Path) -> None:
    path = tmp_path / "records/_ledger" / "research-decisions" / "2026-05.jsonl"
    _write_jsonl(
        path,
        _decision_record(
            decision_event_id="decision-20260501-9682-research",
            decision_scope="research_memo",
            ticker="9682",
            candidate_ref={
                "candidates_ref": "records/04-candidates/2026/05/missing.yaml",
                "candidate_id": "candidate-2026-05-01-9682",
                "screen_run_id": "screening-20260501",
                "ticker": "9682",
            },
        ),
    )

    codes = {finding.code for finding in validate_ledger_file(path)}

    assert "ledger.candidate-ref-missing" in codes


def test_candidate_ref_rejects_absolute_path(tmp_path: Path) -> None:
    outside = tmp_path / "outside.yaml"
    outside.write_text("run_id: screening-20260501\ncandidates: []\n", encoding="utf-8")
    path = tmp_path / "records/_ledger" / "research-decisions" / "2026-05.jsonl"
    _write_jsonl(
        path,
        _decision_record(
            decision_event_id="decision-20260501-9682-research",
            decision_scope="research_memo",
            ticker="9682",
            candidate_ref={
                "candidates_ref": str(outside),
                "candidate_id": "candidate-2026-05-01-9682",
                "screen_run_id": "screening-20260501",
                "ticker": "9682",
            },
        ),
    )

    codes = {finding.code for finding in validate_ledger_file(path)}

    assert "ledger.candidate-ref-path" in codes


def test_candidate_ref_rejects_wrong_target(tmp_path: Path) -> None:
    wrong = tmp_path / "records/02-brief/2026/05/not-candidates.yaml"
    wrong.parent.mkdir(parents=True)
    wrong.write_text("run_id: screening-20260501\ncandidates: []\n", encoding="utf-8")
    path = tmp_path / "records/_ledger" / "research-decisions" / "2026-05.jsonl"
    _write_jsonl(
        path,
        _decision_record(
            decision_event_id="decision-20260501-9682-research",
            decision_scope="research_memo",
            ticker="9682",
            candidate_ref={
                "candidates_ref": "records/02-brief/2026/05/not-candidates.yaml",
                "candidate_id": "candidate-2026-05-01-9682",
                "screen_run_id": "screening-20260501",
                "ticker": "9682",
            },
        ),
    )

    codes = {finding.code for finding in validate_ledger_file(path)}

    assert "ledger.candidate-ref-target" in codes
