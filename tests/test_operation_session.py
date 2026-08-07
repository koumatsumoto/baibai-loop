from __future__ import annotations

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


def _active_payload(checkpoint: str = "source review") -> OperationPayload:
    return OperationPayload(
        checkpoint=checkpoint,
        artifacts=({"kind": "review", "summary": "current reviewed artifact"},),
        human_confirmation={"request": "confirm the reviewed result"},
        next="wait for confirmation",
    )


def _complete_payload(kind: SessionKind) -> OperationPayload:
    values: dict[str, object] = {
        "checkpoint": "final",
        "result": "trigger work completed",
        "next": "wait for the next trigger",
    }
    if kind in {
        "opportunity",
        "pending-result",
        "monthly-contribution",
        "earnings-material-event",
    }:
        values["human_confirmation"] = {
            "request": "confirm",
            "result": "human confirmed",
        }
    if kind in {"opportunity", "earnings-material-event"}:
        values["artifacts"] = ({"kind": "review", "summary": "reviewed"},)
    if kind in {
        "pending-result",
        "monthly-contribution",
        "earnings-material-event",
        "annual-outcome",
    }:
        values["canonical_refs"] = ("canonical-entity-1",)
    return OperationPayload.model_validate(values)


@pytest.mark.parametrize("kind", SESSION_KINDS)
def test_each_kind_resumes_same_row_completes_and_next_occurrence_gets_new_row(
    tmp_path: Path,
    kind: SessionKind,
) -> None:
    service = OperationService(tmp_path / "app.sqlite")
    first = service.start(
        session_kind=kind,
        as_of=date(2026, 7, 19),
        started_at=NOW,
        payload=_active_payload(),
    )

    resumed = service.checkpoint(first.operation_id, _active_payload("review complete"))
    assert resumed.operation_id == first.operation_id
    assert resumed.payload.checkpoint == "review complete"

    completed = service.complete(first.operation_id, _complete_payload(kind), completed_at=NOW)
    assert completed.status == "completed"
    assert completed.completed_at == NOW

    second = service.start(
        session_kind=kind,
        as_of=date(2026, 7, 19),
        started_at=NOW,
        payload=_active_payload(),
    )
    assert second.operation_id != first.operation_id
    assert second.operation_id.endswith("-2")


def test_all_kinds_share_one_active_slot_and_no_checkpoint_history(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    service = OperationService(db)
    operation = service.start(
        session_kind="opportunity",
        as_of=date(2026, 7, 19),
        started_at=NOW,
        payload=_active_payload("one"),
    )

    with pytest.raises(OperationConflictError, match="active operation already exists"):
        service.start(
            session_kind="pending-result",
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


@pytest.mark.parametrize("kind", SESSION_KINDS)
def test_complete_rejects_missing_kind_specific_final_fields(
    tmp_path: Path,
    kind: SessionKind,
) -> None:
    service = OperationService(tmp_path / "app.sqlite")
    operation = service.start(
        session_kind=kind,
        as_of=date(2026, 7, 19),
        started_at=NOW,
        payload=_active_payload(),
    )

    with pytest.raises(OperationCompletionError):
        service.complete(
            operation.operation_id,
            OperationPayload(checkpoint="incomplete"),
            completed_at=NOW,
        )
    assert service.get(operation.operation_id).status == "active"


def test_completed_row_is_immutable_through_service_and_database(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    service = OperationService(db)
    operation = service.start(
        session_kind="annual-outcome",
        as_of=date(2026, 7, 19),
        started_at=NOW,
        payload=_active_payload(),
    )
    service.complete(operation.operation_id, _complete_payload("annual-outcome"), completed_at=NOW)

    with pytest.raises(OperationConflictError, match="immutable"):
        service.checkpoint(operation.operation_id, _active_payload("late update"))
    connection = connect_rw(db)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE operation_session SET payload = '{}' WHERE operation_id = ?",
                (operation.operation_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM operation_session WHERE operation_id = ?", (operation.operation_id,)
            )
    finally:
        connection.close()


def test_database_constraint_rejects_a_second_active_row(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    initialize_database(db)
    service = OperationService(db)
    operation = service.start(
        session_kind="opportunity",
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
                ) VALUES (?, 'annual-outcome', 'active', '2026-07-19', NULL, ?, NULL, ?)
                """,
                (
                    "op-20260719-annual-outcome-1",
                    NOW.isoformat(),
                    operation.payload.model_dump_json(),
                ),
            )
    finally:
        connection.close()


def test_cli_and_read_facade_expose_current_and_completed_payloads(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "app.sqlite"
    assert (
        operation_main(
            [
                "--db",
                str(db),
                "start",
                "--kind",
                "annual-outcome",
                "--as-of",
                "2026-07-19",
            ],
            now=NOW,
        )
        == 0
    )
    started = yaml.safe_load(capsys.readouterr().out)
    operation_id = started["operation_id"]

    final_path = tmp_path / "final.yaml"
    final_path.write_text(
        yaml.safe_dump(_complete_payload("annual-outcome").model_dump(mode="json")),
        encoding="utf-8",
    )
    assert (
        operation_main(
            ["--db", str(db), "complete", operation_id, "--payload", str(final_path)],
            now=NOW,
        )
        == 0
    )
    completed = yaml.safe_load(capsys.readouterr().out)
    assert completed["status"] == "completed"

    assert operation_session(db, operation_id) == completed
    assert list_operation_sessions(db, status="active") == []
    assert [row["operation_id"] for row in list_operation_sessions(db, status="completed")] == [
        operation_id
    ]
