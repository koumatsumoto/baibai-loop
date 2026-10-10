from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from tests.helpers.db_seed import seed_tasks

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.write import connect_rw
from baibai_engine.read_api.sqlite import connect_read_only
from baibai_engine.tasks.cli import main as task_main
from baibai_engine.tasks.models import Task
from baibai_engine.tasks.service import TaskConflictError, TaskService

ROOT = Path(__file__).parents[2]
TASK_FIXTURE = ROOT / "tests/fixtures/task-list/valid.yaml"


def _seed_tasks(db: Path) -> None:
    raw = yaml.safe_load(TASK_FIXTURE.read_text(encoding="utf-8"))
    seed_tasks(db, (Task.model_validate(item) for item in raw["tasks"]))


def test_cli_add_list_done_and_edit_use_current_state_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "app.sqlite"
    today = date(2026, 7, 19)
    assert (
        task_main(
            [
                "--db",
                str(db),
                "add",
                "--title",
                "決算確認",
                "--kind",
                "earnings-review",
                "--due",
                "2026-07-31",
                "--ticker",
                "9715",
            ],
            today=today,
        )
        == 0
    )
    created = yaml.safe_load(capsys.readouterr().out)
    assert created["task_id"] == "task-20260719-9715"
    assert (
        task_main(
            ["--db", str(db), "edit", created["task_id"], "--due", "2026-08-01"],
            today=today,
        )
        == 0
    )
    edited = yaml.safe_load(capsys.readouterr().out)
    assert edited["task_id"] == created["task_id"]
    assert edited["due_date"] == "2026-08-01"
    assert task_main(["--db", str(db), "done", created["task_id"]], today=today) == 0
    done = yaml.safe_load(capsys.readouterr().out)
    assert done["status"] == "done"
    assert done["closed_at"] == today.isoformat()
    assert task_main(["--db", str(db), "list", "--status", "done"], today=today) == 0
    assert len(yaml.safe_load(capsys.readouterr().out)["tasks"]) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [("status", "invalid"), ("due_date", "2026-02-30"), ("unknown", "x")],
)
def test_task_model_rejects_invalid_fields(field: str, value: str) -> None:
    raw = yaml.safe_load(TASK_FIXTURE.read_text())["tasks"][0]
    raw[field] = value
    with pytest.raises(ValidationError):
        Task.model_validate(raw)


def test_read_only_connection_rejects_task_writes(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    _seed_tasks(db)
    connection = connect_read_only(db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM task")
    finally:
        connection.close()
    assert len(TaskService(db).list()) == 1


def test_canonical_json_normalizes_supported_representations() -> None:
    value = {
        "date": date(2026, 7, 19),
        "datetime": datetime.fromisoformat("2026-07-19T12:00:00+09:00"),
        "decimal": Decimal("1.20"),
        "tuple": (1, None),
    }
    assert json.loads(canonical_json(value)) == {
        "date": "2026-07-19",
        "datetime": "2026-07-19T12:00:00+09:00",
        "decimal": "1.20",
        "tuple": [1, None],
    }


def test_task_columns_and_payload_are_updated_together(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    _seed_tasks(db)
    task_id = TaskService(db).list()[0].task_id
    TaskService(db).edit(task_id, {"due_date": date(2027, 1, 1)})
    connection = connect_rw(db)
    try:
        row = connection.execute(
            "SELECT due_date, json_extract(payload, '$.due_date') FROM task WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    finally:
        connection.close()
    assert tuple(row) == ("2027-01-01", "2027-01-01")


def test_append_note_preserves_all_other_fields_and_rejects_replay(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    _seed_tasks(db)
    service = TaskService(db)
    original = service.list()[0]
    body = original.body_md or ""
    digest = hashlib.sha256(body.encode()).hexdigest()
    note = "確認結果\r\n末尾改行も保持\n"

    updated = service.append_note(original.task_id, note, expected_body_sha256=digest)

    assert updated.payload() == {
        **original.payload(),
        "body_md": f"{body}\n\n{note}" if body else note,
    }
    assert service.get(original.task_id) == updated
    with pytest.raises(TaskConflictError, match="body changed"):
        service.append_note(original.task_id, note, expected_body_sha256=digest)
    assert service.get(original.task_id) == updated


@pytest.mark.parametrize("status", ["done", "dropped"])
def test_append_note_cannot_modify_closed_task(tmp_path: Path, status: str) -> None:
    db = tmp_path / "app.sqlite"
    _seed_tasks(db)
    service = TaskService(db)
    original = service.list()[0]
    if status == "done":
        service.close(original.task_id, status="done", closed_at=date(2026, 10, 10))
    else:
        service.close(original.task_id, status="dropped", closed_at=date(2026, 10, 10))
    closed = service.get(original.task_id)
    with pytest.raises(TaskConflictError, match="open task"):
        service.append_note(
            original.task_id,
            "new note",
            expected_body_sha256=hashlib.sha256((original.body_md or "").encode()).hexdigest(),
        )
    assert service.get(original.task_id) == closed


def test_append_note_handles_null_body_and_rejects_blank_note(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    _seed_tasks(db)
    service = TaskService(db)
    original = service.edit(service.list()[0].task_id, {"body_md": None})
    digest = hashlib.sha256(b"").hexdigest()
    with pytest.raises(ValueError, match="blank"):
        service.append_note(original.task_id, " \n\t", expected_body_sha256=digest)
    assert service.get(original.task_id) == original
    assert (
        service.append_note(original.task_id, "note", expected_body_sha256=digest).body_md == "note"
    )


def test_append_note_keeps_existing_payload_compare_and_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "app.sqlite"
    _seed_tasks(db)
    service = TaskService(db)
    other_writer = TaskService(db)
    original = service.list()[0]

    def racing_get(task_id: str) -> Task:
        before = other_writer.get(task_id)
        other_writer.edit(task_id, {"title": "concurrent edit"})
        return before

    monkeypatch.setattr(service, "get", racing_get)
    with pytest.raises(TaskConflictError, match="changed while editing"):
        service.append_note(
            original.task_id,
            "note",
            expected_body_sha256=hashlib.sha256((original.body_md or "").encode()).hexdigest(),
        )
    assert other_writer.get(original.task_id).body_md == original.body_md
    assert other_writer.get(original.task_id).title == "concurrent edit"


def test_cli_append_reads_exact_utf8_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "app.sqlite"
    _seed_tasks(db)
    original = TaskService(db).list()[0]
    note = tmp_path / "note.md"
    note.write_bytes("追記\r\n$(not-a-command)\n".encode())
    assert (
        task_main(
            [
                "--db",
                str(db),
                "edit",
                original.task_id,
                "--append-body-file",
                str(note),
                "--expected-body-sha256",
                hashlib.sha256((original.body_md or "").encode()).hexdigest(),
            ]
        )
        == 0
    )
    updated = yaml.safe_load(capsys.readouterr().out)
    assert updated["body_md"].endswith(note.read_bytes().decode())


@pytest.mark.parametrize(
    "options", [["--due", "2027-01-01"], ["--body", "replace"], ["--clear-body"]]
)
def test_cli_append_rejects_mixed_edits(
    tmp_path: Path, options: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "app.sqlite"
    _seed_tasks(db)
    original = TaskService(db).list()[0]
    assert (
        task_main(
            [
                "--db",
                str(db),
                "edit",
                original.task_id,
                "--append-body-file",
                str(tmp_path / "unused"),
                "--expected-body-sha256",
                "0" * 64,
                *options,
            ]
        )
        == 1
    )
    assert "no other edits" in capsys.readouterr().err
    assert TaskService(db).get(original.task_id) == original


@pytest.mark.parametrize(
    "options", [["--append-body-file", "unused"], ["--expected-body-sha256", "0" * 64]]
)
def test_cli_append_requires_file_and_reviewed_body_hash(
    options: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert task_main(["edit", "task-20261010-ops", *options]) == 1
    assert "requires" in capsys.readouterr().err
