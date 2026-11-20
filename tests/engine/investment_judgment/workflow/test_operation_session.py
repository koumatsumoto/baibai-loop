from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from tests.helpers.db_seed import seed_historical_operation
from tests.helpers.generated_selection import generated_draft_for, seed_generated_selection
from tests.helpers.research_selection import historical_operation, selection

from baibai_engine.cli.operation import main as operation_main
from baibai_engine.investment_judgment.workflow.operation import service as operation_service
from baibai_engine.investment_judgment.workflow.operation.models import (
    SESSION_KINDS,
    OperationHeader,
    OperationPayload,
    OperationSession,
)
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import (
    OperationConflictError,
    OperationService,
)
from baibai_engine.investment_judgment.workflow.selection import selection_sha256
from baibai_engine.primitives.time import JST
from baibai_engine.storage.sqlite.application.write import connect_rw, initialize_database

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=JST)


def test_operation_kinds_are_limited_to_multi_step_workflows(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert SESSION_KINDS == ("capital-allocation", "position-review")
    with pytest.raises(SystemExit):
        operation_main(["start", "--kind", "pending-result", "--as-of", "2026-07-19"], now=NOW)
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.parametrize("generated", [False, True], ids=["legacy", "generated"])
def test_complete_cli_only_adds_notes_and_preserves_saved_selection_and_artifacts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    generated: bool,
) -> None:
    db = tmp_path / "app.sqlite"
    selected = (
        seed_generated_selection(runs=tmp_path / "runs.sqlite", now=NOW, tickers=("1234", "5678"))
        if generated
        else selection(now=NOW, tickers=("1234", "5678"))
    )
    operation = historical_operation(selected)
    payload = operation.payload.model_copy(
        update={
            "artifacts": (*operation.payload.artifacts, {"kind": "research_notes", "ref": "saved"}),
            "canonical_refs": ("saved-research-reference",),
        }
    )
    operation = operation.model_copy(update={"payload": payload})
    seed_historical_operation(db, operation)
    with monkeypatch.context() as patch:
        patch.setattr(
            OperationSession,
            "public",
            lambda _self: pytest.fail(
                "writer must not expand a full public snapshot to replace it"
            ),
        )
        for ticker in ("1234", "5678"):
            operation = OperationService(db).record_unfinished(
                operation.operation_id,
                ticker=ticker,
                reason="一次資料が不足し評価未完了",
                references=(f"synthetic-research-{ticker}", "saved-research-digest"),
                now=NOW,
            )
    payload = operation.payload

    assert (
        operation_main(
            [
                "--db",
                str(db),
                "--format",
                "json",
                "complete",
                operation.operation_id,
                "--result",
                "調査作業を終了、評価は未完了",
                "--next",
                "新しい明示依頼を待つ",
            ],
            now=NOW,
        )
        == 0
    )
    completed = OperationService(db).get(operation.operation_id)
    assert json.loads(capsys.readouterr().out) == completed.model_dump(
        mode="json", exclude={"payload"}
    )
    assert completed.status == "completed"
    assert completed.payload == payload.model_copy(
        update={"result": "調査作業を終了、評価は未完了", "next": "新しい明示依頼を待つ"}
    )
    assert selection_sha256(selection_binding(completed.payload)) == selection_sha256(selected)
    with pytest.raises(OperationConflictError, match="immutable"):
        OperationService(db).complete(
            operation.operation_id, result="別の結果", next="別の予定", completed_at=NOW
        )
    assert OperationService(db).get(operation.operation_id) == completed


def _active_payload(checkpoint: str = "source review", *, selected=None) -> OperationPayload:
    return OperationPayload(
        checkpoint=checkpoint,
        artifacts=(
            (selected if selected is not None else selection(now=NOW)).model_dump(mode="json"),
        ),
        next="continue the reviewed research",
    )


def test_all_kinds_share_one_active_slot_and_no_checkpoint_history(tmp_path: Path) -> None:
    runs = tmp_path / "runs.sqlite"
    selected = seed_generated_selection(runs=runs, now=NOW)
    db = tmp_path / "app.sqlite"
    service = OperationService(db, runs_path=runs)
    operation, _ = service.start(generated_draft_for(selected), now=NOW)

    with pytest.raises(OperationConflictError, match="active operation already exists"):
        service.start(generated_draft_for(selected), now=NOW)

    service.checkpoint(operation.operation_id, note="two", now=NOW)
    with closing(sqlite3.connect(db)) as connection, connection:
        row = connection.execute(
            "SELECT count(*), json_extract(payload, '$.checkpoint') FROM operation_session"
        ).fetchone()
        assert row == (1, "two")
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_schema WHERE type = 'table' "
                "AND name LIKE '%operation%history%'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("field", ["completion_reason", "human_confirmation"])
def test_operation_payload_rejects_retired_workflow_fields(field: str) -> None:
    raw = _active_payload().model_dump(mode="json")
    raw[field] = "no-research" if field == "completion_reason" else {"request": "old workflow"}
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        OperationPayload.model_validate(raw)


def test_database_constraint_rejects_a_second_active_row(tmp_path: Path) -> None:
    runs = tmp_path / "runs.sqlite"
    selected = seed_generated_selection(runs=runs, now=NOW)
    db = tmp_path / "app.sqlite"
    initialize_database(db)
    service = OperationService(db, runs_path=runs)
    operation, _ = service.start(generated_draft_for(selected), now=NOW)
    connection = connect_rw(db)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                """
                INSERT INTO operation_session (
                    operation_id, session_kind, status, as_of, ticker,
                    started_at, completed_at, payload
                ) VALUES (?, 'position-review', 'active', '2026-07-19', NULL, ?, NULL, ?)
                """,
                (
                    "op-20260719-position-review-1",
                    NOW.isoformat(),
                    operation.payload.model_dump_json(),
                ),
            )
    finally:
        connection.close()


def test_operation_list_does_not_create_an_unwritten_database(tmp_path: Path) -> None:
    db = tmp_path / "unwritten.sqlite"
    assert OperationService(db).list() == []
    assert not db.exists()
    db.touch()
    assert OperationService(db).list() == []
    assert db.stat().st_size == 0


def test_operation_lists_sort_by_instant_not_offset_text(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    initialize_database(db)
    records = (
        ("research-op-" + "1" * 32, "2026-07-19T09:00:00+09:00"),
        ("research-op-" + "2" * 32, "2026-07-19T01:00:00+00:00"),
        ("research-op-" + "3" * 32, "2026-07-19T10:00:00+09:00"),
    )
    with closing(connect_rw(db)) as connection, connection:
        connection.executemany(
            "INSERT INTO operation_session "
            "(operation_id, session_kind, status, as_of, ticker, "
            "started_at, completed_at, payload) "
            "VALUES (?, 'capital-allocation', 'completed', '2026-07-19', NULL, ?, ?, ?)",
            [
                (identifier, instant, instant, _active_payload().model_dump_json())
                for identifier, instant in records
            ],
        )
    expected = [identifier for identifier, _ in reversed(records)]
    assert [item.operation_id for item in OperationService(db).list()] == expected
    assert OperationService(db).list(status="active") == []


def test_operation_list_reads_only_headers_and_keeps_full_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "app.sqlite"
    operation = historical_operation(selection(now=NOW, tickers=("1234",)))
    seed_historical_operation(db, operation)
    before = db.read_bytes()
    connect = operation_service.connect_read_only

    def connect_without_payload(path: Path) -> sqlite3.Connection:
        connection = connect(path)
        connection.set_authorizer(
            lambda action, table, column, _database, _source: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ
                and table == "operation_session"
                and column == "payload"
                else sqlite3.SQLITE_OK
            )
        )
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(operation_service, "connect_read_only", connect_without_payload)
        listed = OperationService(db).list(status=operation.status)

    assert len(listed) == 1
    assert type(listed[0]) is OperationHeader
    assert listed[0].public() == operation.model_dump(mode="json", exclude={"payload"})
    assert OperationService(db).get(operation.operation_id) == operation
    assert db.read_bytes() == before


def test_operation_header_is_not_payload_validation(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    operation = historical_operation(selection(now=NOW, tickers=("1234",)))
    seed_historical_operation(db, operation)
    with closing(connect_rw(db)) as connection, connection:
        connection.execute("UPDATE operation_session SET payload = '{}' ")
    before = db.read_bytes()

    assert len(OperationService(db).list()) == 1
    with pytest.raises(ValidationError, match="checkpoint"):
        OperationService(db).get(operation.operation_id)
    assert db.read_bytes() == before


def test_operation_list_still_rejects_invalid_header(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    operation = historical_operation(selection(now=NOW, tickers=("1234",)))
    seed_historical_operation(db, operation)
    with closing(connect_rw(db)) as connection, connection:
        connection.execute("UPDATE operation_session SET started_at = '2026-07-19T12:00:00'")
    before = db.read_bytes()

    with pytest.raises(ValidationError, match="UTC offset"):
        OperationService(db).list()
    assert db.read_bytes() == before


@pytest.mark.parametrize("output_format", ["json", "yaml"])
@pytest.mark.parametrize("status", [None, "active", "completed"])
def test_operation_cli_history_headers_and_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], output_format: str, status: str | None
) -> None:
    db = tmp_path / "app.sqlite"
    operation = historical_operation(selection(now=NOW, tickers=("1234",)))
    seed_historical_operation(db, operation)
    before = db.read_bytes()
    arguments = ["--db", str(db), "--format", output_format, "show"]
    if status is not None:
        arguments.extend(("--status", status))

    assert operation_main(arguments, now=NOW) == 0

    captured = capsys.readouterr()
    expected = (
        [operation.model_dump(mode="json", exclude={"payload"})]
        if status is None or status == operation.status
        else []
    )
    assert yaml.safe_load(captured.out) == {"operations": expected}
    assert captured.err == ""
    assert db.read_bytes() == before


def test_operation_cli_rejects_status_with_id_and_invalid_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "unwritten.sqlite"
    assert operation_main(["--db", str(db), "show", "id", "--status", "active"]) == 1
    assert "--status cannot be combined" in capsys.readouterr().err
    with pytest.raises(SystemExit) as error:
        operation_main(["--db", str(db), "show", "--status", "active' OR 1=1 --"])
    assert error.value.code == 2
    assert not db.exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["start", "--payload", "private"],
        ["checkpoint", "unknown-operation", "--payload", "private"],
    ],
)
def test_retired_raw_payload_commands_fail_before_opening_database(tmp_path, arguments):
    db = tmp_path / "app.sqlite"
    with pytest.raises(SystemExit) as error:
        operation_main(["--db", str(db), *arguments], now=NOW)
    assert error.value.code == 2
    assert not db.exists()


@pytest.mark.parametrize("state", ["corrupt", "obsolete"])
@pytest.mark.parametrize("identifiers", [[], ["unknown-operation"]])
def test_operation_cli_reports_unreadable_stores_without_changing_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], state: str, identifiers: list[str]
) -> None:
    db = tmp_path / "app.sqlite"
    if state == "corrupt":
        db.write_bytes(b"not a SQLite database")
    else:
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.execute("PRAGMA user_version=1")
    before = db.read_bytes()

    assert operation_main(["--db", str(db), "show", *identifiers], now=NOW) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "Traceback" not in captured.err
    assert db.read_bytes() == before


def test_operation_cli_show_preserves_a_retained_legacy_operation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "app.sqlite"
    operation = historical_operation(selection(now=NOW, tickers=("1234",)))
    seed_historical_operation(db, operation)
    before = db.read_bytes()

    assert (
        operation_main(
            ["--db", str(db), "--format", "json", "show", operation.operation_id], now=NOW
        )
        == 0
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == operation.public()
    assert db.read_bytes() == before
