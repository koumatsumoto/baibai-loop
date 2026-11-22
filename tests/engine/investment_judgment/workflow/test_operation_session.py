from __future__ import annotations

import json
import sqlite3
import weakref
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from tests.helpers.db_seed import seed_historical_operation
from tests.helpers.generated_selection import generated_draft_for, seed_generated_selection
from tests.helpers.research_selection import historical_operation, selection

from baibai_engine.cli.investment_judgment import main as investment_judgment_main
from baibai_engine.cli.operation import main as operation_main
from baibai_engine.investment_judgment.workflow.operation import service as operation_service
from baibai_engine.investment_judgment.workflow.operation.models import (
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
from baibai_engine.storage.sqlite.application.schema import APPLICATION_SCHEMA_VERSION
from baibai_engine.storage.sqlite.application.write import connect_rw, initialize_database

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=JST)


@pytest.mark.parametrize("ticker", [None, "", " "])
def test_position_operation_requires_explicit_fixed_selection(
    tmp_path: Path, ticker: str | None
) -> None:
    db = tmp_path / "app.sqlite"
    with pytest.raises(OperationConflictError, match="research_selection"):
        OperationService(db).start(
            session_kind="position-review",
            ticker=ticker,
            as_of=NOW.date(),
            started_at=NOW,
            payload=OperationPayload(checkpoint="start"),
        )
    assert not db.exists()


@pytest.mark.parametrize("tickers", [None, [], ["2331", "2331"]])
def test_start_refuses_incomplete_research_binding_without_creating_db(
    tmp_path: Path, tickers
) -> None:
    db = tmp_path / "app.sqlite"
    payload = OperationPayload(
        checkpoint="start",
        artifacts=()
        if tickers is None
        else ({"kind": "research_triage", "ref": "triage", "research_set": tickers},),
    )
    with pytest.raises(OperationConflictError, match="investment-judgment prepare"):
        OperationService(db).start(
            session_kind="capital-allocation", as_of=NOW.date(), started_at=NOW, payload=payload
        )
    assert not db.exists()


def test_operation_kinds_are_limited_to_multi_step_workflows() -> None:
    from typing import get_args

    from baibai_engine.investment_judgment.workflow.operation.models import SessionKind

    assert get_args(SessionKind) == ("capital-allocation", "position-review")


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

    args = [
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
    ]
    earlier = NOW - timedelta(microseconds=1)
    assert operation_main(args, now=earlier) == 1
    assert "completed_at cannot precede started_at" in capsys.readouterr().err
    assert OperationService(db).get(operation.operation_id) == operation
    assert operation_main(args, now=NOW) == 0
    completed = OperationService(db).get(operation.operation_id)
    assert json.loads(capsys.readouterr().out) == completed.model_dump(
        mode="json", exclude={"payload"}
    )
    assert completed.status == "completed"
    assert completed.payload == payload.model_copy(
        update={"result": "調査作業を終了、評価は未完了", "next": "新しい明示依頼を待つ"}
    )
    assert selection_sha256(selection_binding(completed.payload)) == selection_sha256(selected)
    assert operation_main(args, now=earlier) == 0
    assert json.loads(capsys.readouterr().out) == completed.model_dump(
        mode="json", exclude={"payload"}
    )
    with pytest.raises(OperationConflictError, match="immutable"):
        OperationService(db).complete(
            operation.operation_id, result="別の結果", next="別の予定", completed_at=NOW
        )
    assert OperationService(db).get(operation.operation_id) == completed


def test_operation_json_format_emits_one_machine_object(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "app.sqlite"
    operation = historical_operation(selection(now=NOW))
    seed_historical_operation(db, operation)
    assert (
        operation_main(
            [
                "--db",
                str(db),
                "--format",
                "json",
                "checkpoint",
                operation.operation_id,
                "--note",
                "resume work",
            ],
            now=NOW,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload == operation.model_dump(mode="json", exclude={"payload"})


def _active_payload(checkpoint: str = "source review", *, selected=None) -> OperationPayload:
    return OperationPayload(
        checkpoint=checkpoint,
        artifacts=(
            (selected if selected is not None else selection(now=NOW)).model_dump(mode="json"),
        ),
        next="continue the reviewed research",
    )


def test_start_releases_validated_source_copies_before_saving_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs, app = tmp_path / "runs.sqlite", tmp_path / "app.sqlite"
    selected = seed_generated_selection(
        runs=runs, now=NOW, scope="specified_stocks", tickers=("1234",)
    )
    payload = _active_payload(selected=selected)
    source_before = runs.read_bytes()
    observed = {}
    for name in ("selection_binding", "read_research_model_inputs_from_connection"):
        original = getattr(operation_service, name)

        def read(*args, _original=original, _name=name, **kwargs):
            value = _original(*args, **kwargs)
            observed[_name] = weakref.ref(value)
            return value

        monkeypatch.setattr(operation_service, name, read)
    insert = operation_service._insert

    def save(connection, operation):
        assert observed.keys() == {
            "selection_binding",
            "read_research_model_inputs_from_connection",
        }
        assert all(reference() is None for reference in observed.values())
        assert operation.payload == payload
        insert(connection, operation)

    monkeypatch.setattr(operation_service, "_insert", save)
    operation = OperationService(app, runs_path=runs).start(
        session_kind="capital-allocation", as_of=NOW.date(), started_at=NOW, payload=payload
    )

    assert OperationService(app).get(operation.operation_id) == operation
    assert operation.payload == payload
    assert selection_sha256(selection_binding(operation.payload)) == selection_sha256(selected)
    assert runs.read_bytes() == source_before


def test_all_kinds_share_one_active_slot_and_no_checkpoint_history(tmp_path: Path) -> None:
    runs = tmp_path / "runs.sqlite"
    selected = seed_generated_selection(runs=runs, now=NOW)
    db = tmp_path / "app.sqlite"
    service = OperationService(db, runs_path=runs)
    operation = service.start(
        session_kind="capital-allocation",
        as_of=date(2026, 7, 19),
        started_at=NOW,
        payload=_active_payload("one", selected=selected),
    )

    with pytest.raises(OperationConflictError, match="active operation already exists"):
        service.start(
            session_kind="capital-allocation",
            ticker=None,
            as_of=date(2026, 7, 19),
            started_at=NOW,
            payload=_active_payload(selected=selected),
        )

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
    operation = service.start(
        session_kind="capital-allocation",
        as_of=date(2026, 7, 19),
        started_at=NOW,
        payload=_active_payload(selected=selected),
    )
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


@pytest.mark.parametrize("app_state", ["zero_byte", "empty_sqlite", "missing_required_table"])
def test_prepare_distinguishes_unwritten_app_from_incomplete_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], app_state: str
) -> None:
    db = tmp_path / "app.sqlite"
    if app_state == "zero_byte":
        db.touch()
    else:
        with closing(sqlite3.connect(db)) as connection:
            if app_state == "empty_sqlite":
                connection.execute("VACUUM")
            else:
                connection.execute(f"PRAGMA user_version={APPLICATION_SCHEMA_VERSION}")
    before_db = db.read_bytes()
    runs = tmp_path / "runs.sqlite"
    selected = seed_generated_selection(
        runs=runs, now=NOW, tickers=("1234",), scope="specified_stocks"
    )
    draft = tmp_path / "selection.json"
    draft.write_text(generated_draft_for(selected).model_dump_json(), encoding="utf-8")
    input_bytes = runs.read_bytes(), draft.read_bytes()
    workspace = tmp_path / "workspace"
    args = [
        "prepare",
        "--selection-file",
        str(draft),
        "--db",
        str(db),
        "--runs-path",
        str(runs),
        "--workspace",
        str(workspace),
    ]

    result = investment_judgment_main(args, now=NOW)
    body = json.loads(capsys.readouterr().out)
    assert (runs.read_bytes(), draft.read_bytes()) == input_bytes
    if app_state == "missing_required_table":
        assert result == 3
        assert body["status"] == "invalid_source"
        assert "no such table: operation_session" in body["reason"]
        assert db.read_bytes() == before_db
        assert not workspace.exists()
        return

    assert result == 0, body
    assert body["status"] == "prepared"
    active = OperationService(db).active()
    assert active is not None
    assert active.operation_id == body["operation_id"]
    assert (workspace / "domain-inputs.yaml").is_file()
    saved = db.read_bytes()
    assert investment_judgment_main(args, now=NOW) == 0
    retried = json.loads(capsys.readouterr().out)
    assert retried["status"] == "resumed"
    assert retried["operation_id"] == body["operation_id"]
    assert retried["selection_sha256"] == body["selection_sha256"]
    assert db.read_bytes() == saved


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
    ("arguments", "reason"),
    [
        (
            ["start", "--kind", "capital-allocation", "--as-of", "2026-07-19"],
            "invalid choice: 'start'",
        ),
        (
            ["checkpoint", "unknown-operation", "--note", "a note"],
            "unrecognized arguments: --payload",
        ),
    ],
    ids=["start", "checkpoint"],
)
def test_removed_operation_payload_arguments_never_open_a_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], arguments: list[str], reason: str
) -> None:
    db = tmp_path / "app.sqlite"
    draft = tmp_path / "payload.yaml"
    draft.write_text("checkpoint: [\nprivate-marker: do-not-echo\n", encoding="utf-8")

    with pytest.raises(SystemExit) as error:
        operation_main(["--db", str(db), *arguments, "--payload", str(draft)], now=NOW)
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert reason in captured.err
    assert "do-not-echo" not in captured.err
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
