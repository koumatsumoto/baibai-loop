from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pytest
import yaml

from baibai_engine.appdb.write import connect_rw, initialize_database
from baibai_engine.foundation.time import JST
from baibai_engine.operation.cli import main as operation_main
from baibai_engine.operation.models import SESSION_KINDS, OperationPayload, SessionKind
from baibai_engine.operation.service import (
    OperationCompletionError,
    OperationConflictError,
    OperationService,
)
from baibai_engine.read_api.operations import list_operation_sessions, operation_session

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=JST)


@pytest.mark.parametrize("ticker", [None, "", " "])
def test_position_operation_is_retired_before_write(tmp_path: Path, ticker: str | None) -> None:
    db = tmp_path / "app.sqlite"
    with pytest.raises(OperationConflictError, match="no Operation is needed"):
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
    with pytest.raises(OperationConflictError, match="research prepare"):
        OperationService(db).start(
            session_kind="capital-allocation", as_of=NOW.date(), started_at=NOW, payload=payload
        )
    assert not db.exists()


def test_operation_kinds_are_limited_to_multi_step_workflows(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert SESSION_KINDS == ("capital-allocation", "position-review")
    with pytest.raises(SystemExit):
        operation_main(["start", "--kind", "pending-result", "--as-of", "2026-07-19"], now=NOW)
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["start", "checkpoint", "complete"])
def test_operation_payload_help_names_a_file_path(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        operation_main([command, "--help"])

    assert error.value.code == 0
    assert "path to an OperationPayload YAML or JSON file" in capsys.readouterr().out


def test_operation_json_format_emits_one_machine_object(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "app.sqlite"
    draft = tmp_path / "start.yaml"
    draft.write_text(yaml.safe_dump(_active_payload().model_dump(mode="json")))

    assert (
        operation_main(
            [
                "--db",
                str(db),
                "--format",
                "json",
                "start",
                "--kind",
                "capital-allocation",
                "--payload",
                str(draft),
                "--as-of",
                "2026-07-19",
            ],
            now=NOW,
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["session_kind"] == "capital-allocation"


def _active_payload(checkpoint: str = "source review") -> OperationPayload:
    return OperationPayload(
        checkpoint=checkpoint,
        artifacts=({"kind": "research_triage", "ref": "triage-test", "research_set": ["2331"]},),
        human_confirmation={"request": "confirm the reviewed result"},
        next="wait for confirmation",
    )


def _complete_payload(kind: SessionKind) -> OperationPayload:
    values: dict[str, object] = {
        "checkpoint": "final",
        "result": "trigger work completed",
        "next": "wait for the next trigger",
    }
    values["human_confirmation"] = {
        "request": "confirm",
        "result": "human confirmed",
    }
    values["artifacts"] = ({"kind": "review", "summary": "reviewed"},)
    if kind == "position-review":
        values["canonical_refs"] = ("position-review-test",)
    return OperationPayload.model_validate(values)


def test_all_kinds_share_one_active_slot_and_no_checkpoint_history(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    service = OperationService(db)
    operation = service.start(
        session_kind="capital-allocation",
        as_of=date(2026, 7, 19),
        started_at=NOW,
        payload=_active_payload("one"),
    )

    with pytest.raises(OperationConflictError, match="active operation already exists"):
        service.start(
            session_kind="capital-allocation",
            ticker="2331",
            as_of=date(2026, 7, 19),
            started_at=NOW,
            payload=_active_payload(),
        )

    service.checkpoint(operation.operation_id, _active_payload("two"))
    with sqlite3.connect(db) as connection:
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


@pytest.mark.parametrize("kind", ["capital-allocation"])
def test_complete_rejects_missing_kind_specific_final_fields(
    tmp_path: Path,
    kind: SessionKind,
) -> None:
    service = OperationService(tmp_path / "app.sqlite")
    operation = service.start(
        session_kind=kind,
        ticker="2331",
        as_of=date(2026, 7, 19),
        started_at=NOW,
        payload=_active_payload(),
    )

    with pytest.raises(OperationCompletionError):
        service.complete(
            operation.operation_id,
            OperationPayload(checkpoint="incomplete", artifacts=operation.payload.artifacts),
            completed_at=NOW,
        )
    assert service.get(operation.operation_id).status == "active"


@pytest.mark.parametrize("kind", ["capital-allocation"])
def test_no_research_is_readable_history_but_rejected_by_all_writers(
    tmp_path: Path, kind: SessionKind, subtests: pytest.Subtests
) -> None:
    db = tmp_path / "app.sqlite"
    service = OperationService(db)
    historical = OperationPayload(
        checkpoint="final",
        completion_reason="no-research",
        result="all skipped",
        next="next triage",
        artifacts=({"kind": "research_triage", "ref": "triage-old", "research_count": 0},),
        canonical_refs=("triage-old",),
    )
    with pytest.raises(OperationCompletionError, match="read-only history"):
        service.start(
            session_kind=kind, ticker="2331", as_of=NOW.date(), started_at=NOW, payload=historical
        )
    operation = service.start(
        session_kind=kind,
        ticker="2331",
        as_of=NOW.date(),
        started_at=NOW,
        payload=_active_payload(),
    )
    for command, invoke in (
        ("checkpoint", lambda: service.checkpoint(operation.operation_id, historical)),
        (
            "complete",
            lambda: service.complete(operation.operation_id, historical, completed_at=NOW),
        ),
    ):
        with subtests.test(command=command):
            with pytest.raises(OperationCompletionError, match="read-only history"):
                invoke()
            assert service.get(operation.operation_id) == operation
    # Seed an already completed row exactly as it was persisted before writer retirement.
    with connect_rw(db) as connection:
        connection.execute(
            "UPDATE operation_session SET status='completed', completed_at=?, payload=? "
            "WHERE operation_id=?",
            (NOW.isoformat(), historical.model_dump_json(), operation.operation_id),
        )
    assert service.get(operation.operation_id).payload == historical
    assert operation_session(db, operation.operation_id) is not None
    assert len(list_operation_sessions(db)) == 1


def test_database_constraint_rejects_a_second_active_row(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    initialize_database(db)
    service = OperationService(db)
    operation = service.start(
        session_kind="capital-allocation",
        as_of=date(2026, 7, 19),
        started_at=NOW,
        payload=_active_payload(),
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


@pytest.mark.parametrize("status", ["active", "completed"])
def test_position_operation_history_remains_readable_and_immutable(tmp_path, status):
    db = tmp_path / "app.sqlite"
    initialize_database(db)
    with connect_rw(db) as connection:
        connection.execute(
            "INSERT INTO operation_session VALUES (?,?,?,?,?,?,?,?)",
            (
                "op-20260719-position-review-1",
                "position-review",
                status,
                NOW.date().isoformat(),
                "2331",
                NOW.isoformat(),
                NOW.isoformat() if status == "completed" else None,
                _active_payload().model_dump_json(),
            ),
        )
    service = OperationService(db)
    assert service.get("op-20260719-position-review-1").session_kind == "position-review"
    assert operation_session(db, "op-20260719-position-review-1")["status"] == status
    assert len(list_operation_sessions(db)) == 1
    with pytest.raises(OperationConflictError, match=r"immutable|read-only history"):
        service.checkpoint("op-20260719-position-review-1", _active_payload("changed"))
    if status == "completed":
        with connect_rw(db) as connection, pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM operation_session WHERE operation_id='op-20260719-position-review-1'"
            )
