from __future__ import annotations

import hashlib
import json
from pathlib import Path

from baibai_loop.validate.migration_manifest import validate_migration_manifest_file


def _write_manifest(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )


def _row(**overrides: object) -> dict[str, object]:
    old_row_raw = json.dumps({"ledger_id": "old-1"}, sort_keys=True)
    row: dict[str, object] = {
        "migration_run_id": "migration-domain-model-20260506",
        "migration_strategy": "merge",
        "granularity": "row",
        "old_path": "records/_ledger/paper/2026-05.jsonl",
        "old_row_raw": old_row_raw,
        "old_row_sha256": "sha256:" + hashlib.sha256(old_row_raw.encode("utf-8")).hexdigest(),
        "old_line_number": 1,
        "old_ledger_id": "old-1",
        "new_path": "records/_ledger/research-decisions/2026-05.jsonl",
        "new_row_sha256": "sha256:" + "b" * 64,
        "new_line_number": 1,
        "new_decision_id": "decision-1",
        "consolidation_group_id": "group-1",
        "schema_from": "ledger-v1",
        "schema_to": "decision-register-v1",
        "migration_tool": "baibai-loop-migrate-domain-model",
    }
    row.update(overrides)
    return row


def _summary_row() -> dict[str, object]:
    return {
        "migration_run_id": "migration-domain-model-20260506",
        "migration_strategy": "summarize",
        "granularity": "repository",
        "unregistered_candidate_population": {
            "universe_size": 380,
            "screened_population": 380,
            "hit_population": 6,
            "reviewed_population": 6,
        },
    }


def test_repository_manifest_passes() -> None:
    findings = validate_migration_manifest_file(
        Path("records/_migrations/2026-05-06-domain-model.jsonl")
    )
    assert findings == []


def test_requires_row_level_audit_rows(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    _write_manifest(manifest, [_summary_row()])
    codes = {finding.code for finding in validate_migration_manifest_file(manifest)}
    assert "migration-manifest.row-granularity" in codes


def test_requires_unregistered_candidate_population_summary(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    _write_manifest(manifest, [_row()])
    codes = {finding.code for finding in validate_migration_manifest_file(manifest)}
    assert "migration-manifest.unregistered-summary" in codes


def test_row_level_mapping_requires_hashes_and_ids(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    broken = _row()
    del broken["old_row_sha256"]
    del broken["new_decision_id"]
    _write_manifest(manifest, [broken, _summary_row()])
    codes = {finding.code for finding in validate_migration_manifest_file(manifest)}
    assert "migration-manifest.row-required" in codes


def test_row_level_mapping_checks_new_row_hash(tmp_path: Path) -> None:
    row_line = json.dumps({"decision_event_id": "decision-1"}, sort_keys=True)
    target = tmp_path / "records/_ledger/research-decisions/2026-05.jsonl"
    target.parent.mkdir(parents=True)
    target.write_text(row_line + "\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    manifest = tmp_path / "records/_migrations/manifest.jsonl"
    manifest.parent.mkdir(parents=True)
    _write_manifest(manifest, [_row(new_row_sha256="sha256:" + "c" * 64), _summary_row()])

    codes = {finding.code for finding in validate_migration_manifest_file(manifest)}

    assert "migration-manifest.new-row-hash" in codes


def test_row_level_mapping_checks_new_decision_id(tmp_path: Path) -> None:
    row_line = json.dumps({"decision_event_id": "decision-1"}, sort_keys=True)
    target = tmp_path / "records/_ledger/research-decisions/2026-05.jsonl"
    target.parent.mkdir(parents=True)
    target.write_text(row_line + "\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    manifest = tmp_path / "records/_migrations/manifest.jsonl"
    manifest.parent.mkdir(parents=True)
    digest = "sha256:" + hashlib.sha256(row_line.encode("utf-8")).hexdigest()
    _write_manifest(
        manifest,
        [_row(new_row_sha256=digest, new_decision_id="decision-other"), _summary_row()],
    )

    codes = {finding.code for finding in validate_migration_manifest_file(manifest)}

    assert "migration-manifest.new-decision-id" in codes


def test_row_level_mapping_requires_sha256_format(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    _write_manifest(manifest, [_row(old_row_sha256="bad", new_row_sha256="bad"), _summary_row()])

    codes = {finding.code for finding in validate_migration_manifest_file(manifest)}

    assert "migration-manifest.sha256-format" in codes


def test_row_level_mapping_checks_old_row_hash(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    _write_manifest(manifest, [_row(old_row_sha256="sha256:" + "c" * 64), _summary_row()])

    codes = {finding.code for finding in validate_migration_manifest_file(manifest)}

    assert "migration-manifest.old-row-hash" in codes


def test_row_level_mapping_checks_old_path_reference(tmp_path: Path) -> None:
    old_row = json.dumps({"ledger_id": "old-1", "mutated": True}, sort_keys=True)
    old_path = tmp_path / "records/_ledger/paper/2026-05.jsonl"
    old_path.parent.mkdir(parents=True)
    old_path.write_text(old_row + "\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    manifest = tmp_path / "records/_migrations/manifest.jsonl"
    manifest.parent.mkdir(parents=True)
    _write_manifest(manifest, [_row(), _summary_row()])

    codes = {finding.code for finding in validate_migration_manifest_file(manifest)}

    assert "migration-manifest.old-row-reference-raw" in codes
    assert "migration-manifest.old-row-reference-hash" in codes


def test_row_level_mapping_requires_resolvable_old_path_reference(tmp_path: Path) -> None:
    (tmp_path / "records/_migrations").mkdir(parents=True)
    (tmp_path / "src").mkdir()
    manifest = tmp_path / "records/_migrations/manifest.jsonl"
    _write_manifest(manifest, [_row(), _summary_row()])

    codes = {finding.code for finding in validate_migration_manifest_file(manifest)}

    assert "migration-manifest.old-row-reference-missing" in codes


def test_row_level_mapping_rejects_typed_old_reference_bypass(tmp_path: Path) -> None:
    old_path = tmp_path / "records/_ledger/paper/2026-05.jsonl"
    old_path.parent.mkdir(parents=True)
    old_path.write_text(json.dumps({"ledger_id": "old-1"}, sort_keys=True) + "\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    manifest = tmp_path / "records/_migrations/manifest.jsonl"
    manifest.parent.mkdir(parents=True)
    _write_manifest(manifest, [_row(old_line_number="1"), _summary_row()])

    codes = {finding.code for finding in validate_migration_manifest_file(manifest)}

    assert "migration-manifest.old-row-reference-type" in codes


def test_row_level_mapping_rejects_disallowed_old_path(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    manifest = tmp_path / "records/_migrations/manifest.jsonl"
    manifest.parent.mkdir(parents=True)
    _write_manifest(
        manifest,
        [_row(old_path="records/_migrations/fabricated-source.jsonl"), _summary_row()],
    )

    codes = {finding.code for finding in validate_migration_manifest_file(manifest)}

    assert "migration-manifest.old-row-reference-path" in codes


def test_row_level_mapping_does_not_use_current_worktree_old_path_in_git_repo(
    tmp_path: Path,
) -> None:
    old_path = tmp_path / "records/_ledger/paper/2026-05.jsonl"
    old_path.parent.mkdir(parents=True)
    old_path.write_text(json.dumps({"ledger_id": "old-1"}, sort_keys=True) + "\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / ".git").mkdir()
    manifest = tmp_path / "records/_migrations/manifest.jsonl"
    manifest.parent.mkdir(parents=True)
    _write_manifest(manifest, [_row(), _summary_row()])

    codes = {finding.code for finding in validate_migration_manifest_file(manifest)}

    assert "migration-manifest.old-row-reference-missing" in codes


def test_row_level_mapping_checks_old_ledger_id(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    _write_manifest(manifest, [_row(old_ledger_id="other"), _summary_row()])

    codes = {finding.code for finding in validate_migration_manifest_file(manifest)}

    assert "migration-manifest.old-ledger-id" in codes
