from __future__ import annotations

from pathlib import Path

from baibai_loop.validation.cli import main as validation_main
from baibai_loop.validation.task_list import (
    discover_task_list_files,
    validate_task_list_file,
)

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests/fixtures/task-list"


def test_valid_task_list_passes() -> None:
    assert validate_task_list_file(FIXTURES / "valid.yaml") == []


def test_status_outside_enum_fails() -> None:
    findings = validate_task_list_file(FIXTURES / "invalid-status.yaml")

    assert {finding.code for finding in findings} == {"task-list.enum"}


def test_invalid_calendar_date_fails() -> None:
    findings = validate_task_list_file(FIXTURES / "invalid-date.yaml")

    assert {finding.code for finding in findings} == {"task-list.format"}


def test_invalid_optional_calendar_dates_fail() -> None:
    findings = validate_task_list_file(FIXTURES / "invalid-optional-dates.yaml")

    assert {finding.code for finding in findings} == {"task-list.anyOf"}
    assert {finding.location for finding in findings} == {
        "tasks[0].event_date",
        "tasks[1].closed_at",
    }


def test_duplicate_task_id_fails() -> None:
    findings = validate_task_list_file(FIXTURES / "duplicate-id.yaml")

    assert {finding.code for finding in findings} == {"task-list.duplicate-task-id"}


def test_discovery_only_accepts_canonical_task_list_name(tmp_path: Path) -> None:
    root = tmp_path / "records/05-task"
    root.mkdir(parents=True)
    (root / "other.yaml").write_text("schema_version: 1\ntasks: []\n", encoding="utf-8")
    assert discover_task_list_files(root) == []

    canonical = root / "tasks.yaml"
    canonical.write_text("schema_version: 1\ntasks: []\n", encoding="utf-8")
    assert discover_task_list_files(root) == [canonical]


def test_validation_cli_includes_task_list_target(tmp_path: Path) -> None:
    path = tmp_path / "records/05-task/tasks.yaml"
    path.parent.mkdir(parents=True)
    path.write_text((FIXTURES / "valid.yaml").read_text(encoding="utf-8"), encoding="utf-8")

    assert validation_main(["--root", str(tmp_path), "--target", "task-list"]) == 0
