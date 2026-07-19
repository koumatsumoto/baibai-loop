from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.write import connect_rw
from baibai_engine.read_api.sqlite import connect_read_only
from baibai_engine.tasks.cli import main as task_main
from baibai_engine.tasks.importer import import_task_file
from baibai_engine.tasks.models import Task
from baibai_engine.tasks.service import TaskConflictError, TaskService

ROOT = Path(__file__).parents[1]
TASKS = ROOT / "records/05-task/tasks.yaml"


def test_import_is_create_only_idempotent_and_preserves_all_fields(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"

    assert import_task_file(TASKS, db_path=db) == (8, 0)
    assert import_task_file(TASKS, db_path=db) == (0, 8)

    legacy = yaml.safe_load(TASKS.read_text(encoding="utf-8"))["tasks"]
    imported = [task.payload() for task in TaskService(db).list()]
    assert sorted(imported, key=lambda item: str(item["task_id"])) == sorted(
        legacy, key=lambda item: str(item["task_id"])
    )


def test_import_conflict_rolls_back_the_whole_domain(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    import_task_file(TASKS, db_path=db)
    raw = yaml.safe_load(TASKS.read_text(encoding="utf-8"))
    raw["tasks"][0]["title"] = "conflict"
    raw["tasks"].append(
        {
            **raw["tasks"][0],
            "task_id": "task-20260719-new-task",
            "title": "new row that must roll back",
        }
    )
    source = tmp_path / "conflict.yaml"
    source.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    with pytest.raises(TaskConflictError):
        import_task_file(source, db_path=db)

    assert len(TaskService(db).list()) == 8
    with pytest.raises(ValueError, match="unknown task_id"):
        TaskService(db).get("task-20260719-new-task")


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
def test_task_model_rejects_legacy_schema_negative_cases(field: str, value: str) -> None:
    raw = yaml.safe_load((ROOT / "tests/fixtures/task-list/valid.yaml").read_text())["tasks"][0]
    raw[field] = value
    with pytest.raises(ValidationError):
        Task.model_validate(raw)


def test_read_only_connection_rejects_task_writes(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    import_task_file(TASKS, db_path=db)
    connection = connect_read_only(db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM task")
    finally:
        connection.close()
    assert len(TaskService(db).list()) == 8


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
    import_task_file(TASKS, db_path=db)
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
