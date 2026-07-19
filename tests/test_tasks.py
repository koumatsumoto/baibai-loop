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
from baibai_engine.tasks.models import Task
from baibai_engine.tasks.service import TaskService
from tests.helpers.db_seed import seed_tasks

ROOT = Path(__file__).parents[1]
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
