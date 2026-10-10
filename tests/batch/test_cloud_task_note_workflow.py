from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
import yaml

from baibai_engine.tasks.service import TaskService

ROOT = Path(__file__).resolve().parents[2]


def _workflow() -> dict:
    return yaml.load(
        (ROOT / ".github/workflows/cloud-task-note.yml").read_text(), Loader=yaml.BaseLoader
    )


def _step(step_id: str) -> dict:
    return next(
        step for step in _workflow()["jobs"]["append"]["steps"] if step.get("id") == step_id
    )


def _run_step(
    tmp_path: Path, step_id: str, extra_env: dict[str, str]
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", _step(step_id)["run"]],
        cwd=tmp_path,
        env={**os.environ, "RUNNER_TEMP": str(tmp_path), **extra_env},
        capture_output=True,
        text=True,
        check=False,
    )


def test_note_workflow_is_manual_main_only_and_keeps_publication_separate() -> None:
    workflow = _workflow()
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert set(workflow["on"]["workflow_dispatch"]["inputs"]) == {
        "task_id",
        "expected_body_sha256",
        "note_sha256",
    }
    assert workflow["jobs"]["append"]["if"] == "github.ref == 'refs/heads/main'"
    assert workflow["permissions"] == {"contents": "read"}
    steps = workflow["jobs"]["append"]["steps"]
    ids = [step.get("id") for step in steps if "id" in step]
    assert ids == ["note", "pull", "edit", "push"]
    assert _step("pull")["run"] == "batch/scripts/r2_transfer.sh pull-app"
    assert "batch/scripts/r2_transfer.sh push-app" in _step("push")["run"]
    assert set(_step("edit")["env"]) == {"TASK_ID", "EXPECTED_BODY_SHA256"}
    assert all("materialize" not in str(step.get("run", "")) for step in steps)
    assert steps[-1]["if"] == "always()"
    assert "task-note-edit.log" in steps[-1]["run"]


@pytest.mark.parametrize("correct_digest", [True, False])
def test_staging_pins_secret_bytes_without_executing_or_logging_them(
    tmp_path: Path, correct_digest: bool
) -> None:
    note = "private 日本語\r\n$(touch injected)\n"
    digest = hashlib.sha256(note.encode()).hexdigest() if correct_digest else "0" * 64
    result = _run_step(tmp_path, "note", {"TASK_NOTE": note, "NOTE_SHA256": digest})
    assert result.returncode == (0 if correct_digest else 1)
    assert (tmp_path / "task-note.md").read_bytes() == note.encode()
    assert (tmp_path / "task-note.md").stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "injected").exists()
    assert "private" not in result.stdout + result.stderr


def test_staging_rejects_absent_secret_even_with_empty_digest(tmp_path: Path) -> None:
    result = _run_step(
        tmp_path, "note", {"TASK_NOTE": "", "NOTE_SHA256": hashlib.sha256(b"").hexdigest()}
    )
    assert result.returncode == 1


@pytest.mark.parametrize("exit_code", [0, 1])
def test_edit_never_logs_private_cli_output(tmp_path: Path, exit_code: int) -> None:
    stub = tmp_path / "uv"
    stub.write_text(
        f"#!/bin/sh\nprintf 'private task body\\n'\nprintf 'private validation input\\n' >&2\nexit {exit_code}\n"
    )
    stub.chmod(0o700)
    result = _run_step(
        tmp_path,
        "edit",
        {
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "TASK_ID": "task-20261010-ops",
            "EXPECTED_BODY_SHA256": "0" * 64,
        },
    )
    assert result.returncode == exit_code
    assert "private" not in result.stdout + result.stderr
    log = tmp_path / "task-note-edit.log"
    assert "private task body" in log.read_text()
    assert "private validation input" in log.read_text()
    assert log.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("injected_field", ["TASK_ID", "EXPECTED_BODY_SHA256", None])
def test_edit_dispatch_values_cannot_become_cli_options(
    tmp_path: Path, injected_field: str | None
) -> None:
    db = tmp_path / "stores/application/baibai.sqlite"
    db.parent.mkdir(parents=True)
    service = TaskService(db)
    original = service.add(
        title="fixture", kind="ops", due_date=date(2026, 10, 17), created_at=date(2026, 10, 10)
    )
    (tmp_path / "task-note.md").write_text("private note\n")
    # Replace only uv's project discovery; execute the real CLI against the fixture.
    stub = tmp_path / "uv"
    stub.write_text(
        f"#!{sys.executable}\nimport sys\nfrom baibai_engine.tasks.cli import main\n"
        "raise SystemExit(main(sys.argv[4:]))\n"
    )
    stub.chmod(0o700)
    env = {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "TASK_ID": original.task_id,
        "EXPECTED_BODY_SHA256": hashlib.sha256(b"").hexdigest(),
    }
    if injected_field is not None:
        env[injected_field] = "--help"
    result = _run_step(tmp_path, "edit", env)
    assert result.returncode == (0 if injected_field is None else 1)
    assert "private note" not in result.stdout + result.stderr
    if injected_field is None:
        assert service.get(original.task_id).body_md == "private note\n"
    else:
        assert service.get(original.task_id) == original
