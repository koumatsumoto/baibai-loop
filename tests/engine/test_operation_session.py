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


@pytest.mark.parametrize("command", ["start", "checkpoint", "complete"])
def test_operation_payload_help_names_a_file_path(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        operation_main([command, "--help"])

    assert error.value.code == 0
    assert "path to an OperationPayload YAML or JSON file" in capsys.readouterr().out


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


def test_opportunity_with_no_shortlist_selection_completes_without_human_confirmation(
    tmp_path: Path,
) -> None:
    service = OperationService(tmp_path / "app.sqlite")
    operation = service.start(
        session_kind="opportunity",
        as_of=date(2026, 7, 19),
        started_at=NOW,
        payload=_active_payload(),
    )
    with connect_rw(tmp_path / "app.sqlite") as connection:
        connection.execute(
            """
            INSERT INTO shortlist (
                shortlist_id, selection_id, run_revision_id, as_of, published_at, payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "shortlist-1",
                "selection-1",
                "run-1",
                "2026-07-19",
                NOW.isoformat(),
                json.dumps(
                    {
                        "kind": "shortlist",
                        "shortlist_id": "shortlist-1",
                        "as_of": "2026-07-19",
                        "entries": [{"ticker": "2331", "decision": "rejected"}],
                    }
                ),
            ),
        )
    payload = OperationPayload(
        checkpoint="shortlist cycle complete",
        artifacts=({"kind": "shortlist", "ref": "shortlist-1", "selected_count": 0},),
        canonical_refs=("shortlist-1",),
        completion_reason="no-shortlist-selection",
        result="no candidate qualified for primary research",
        next="wait for the next opportunity trigger",
    )

    completed = service.complete(operation.operation_id, payload, completed_at=NOW)

    assert completed.status == "completed"
    assert completed.payload.human_confirmation is None
    assert completed.payload.completion_reason == "no-shortlist-selection"


@pytest.mark.parametrize(
    "payload",
    [
        OperationPayload(
            checkpoint="final",
            artifacts=({"kind": "shortlist", "ref": "shortlist-1", "selected_count": 1},),
            canonical_refs=("shortlist-1",),
            completion_reason="no-shortlist-selection",
            result="done",
            next="wait",
        ),
        OperationPayload(
            checkpoint="final",
            artifacts=({"kind": "shortlist", "ref": "shortlist-1", "selected_count": 0},),
            canonical_refs=("shortlist-1",),
            human_confirmation={"request": "confirm", "result": "not applicable"},
            completion_reason="no-shortlist-selection",
            result="done",
            next="wait",
        ),
    ],
)
def test_no_shortlist_selection_completion_rejects_false_evidence(
    tmp_path: Path,
    payload: OperationPayload,
) -> None:
    service = OperationService(tmp_path / "app.sqlite")
    operation = service.start(
        session_kind="opportunity",
        as_of=date(2026, 7, 19),
        started_at=NOW,
        payload=_active_payload(),
    )

    with pytest.raises(OperationCompletionError):
        service.complete(operation.operation_id, payload, completed_at=NOW)


def test_no_shortlist_selection_reason_is_opportunity_only(tmp_path: Path) -> None:
    service = OperationService(tmp_path / "app.sqlite")
    operation = service.start(
        session_kind="annual-outcome",
        as_of=date(2026, 7, 19),
        started_at=NOW,
        payload=_active_payload(),
    )
    payload = OperationPayload(
        checkpoint="final",
        artifacts=({"kind": "shortlist", "selected_count": 0},),
        canonical_refs=("shortlist-1",),
        completion_reason="no-shortlist-selection",
        result="done",
        next="wait",
    )

    with pytest.raises(OperationCompletionError, match="completion_reason"):
        service.complete(operation.operation_id, payload, completed_at=NOW)


@pytest.mark.parametrize(
    ("shortlist_as_of", "decision", "message"),
    [
        (None, None, "canonical shortlist"),
        ("2026-07-18", "rejected", "canonical shortlist"),
        ("2026-07-19", "selected", "zero selected entries"),
    ],
)
def test_no_shortlist_selection_completion_checks_the_canonical_publication(
    tmp_path: Path,
    shortlist_as_of: str | None,
    decision: str | None,
    message: str,
) -> None:
    db = tmp_path / "app.sqlite"
    service = OperationService(db)
    operation = service.start(
        session_kind="opportunity",
        as_of=date(2026, 7, 19),
        started_at=NOW,
        payload=_active_payload(),
    )
    if shortlist_as_of is not None and decision is not None:
        with connect_rw(db) as connection:
            connection.execute(
                """
                INSERT INTO shortlist (
                    shortlist_id, selection_id, run_revision_id, as_of, published_at, payload
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "shortlist-1",
                    "selection-1",
                    "run-1",
                    shortlist_as_of,
                    NOW.isoformat(),
                    json.dumps(
                        {
                            "kind": "shortlist",
                            "shortlist_id": "shortlist-1",
                            "as_of": shortlist_as_of,
                            "entries": [{"ticker": "2331", "decision": decision}],
                        }
                    ),
                ),
            )
    payload = OperationPayload(
        checkpoint="final",
        artifacts=({"kind": "shortlist", "ref": "shortlist-1", "selected_count": 0},),
        canonical_refs=("shortlist-1",),
        completion_reason="no-shortlist-selection",
        result="none",
        next="wait",
    )

    with pytest.raises(OperationCompletionError, match=message):
        service.complete(operation.operation_id, payload, completed_at=NOW)
    assert service.get(operation.operation_id).status == "active"


def test_no_shortlist_selection_checks_the_canonical_artifact_reference(
    tmp_path: Path,
) -> None:
    db = tmp_path / "app.sqlite"
    service = OperationService(db)
    operation = service.start(
        session_kind="opportunity",
        as_of=date(2026, 7, 19),
        started_at=NOW,
        payload=_active_payload(),
    )
    with connect_rw(db) as connection:
        connection.execute(
            """
            INSERT INTO shortlist (
                shortlist_id, selection_id, run_revision_id, as_of, published_at, payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "shortlist-real",
                "selection-1",
                "run-1",
                "2026-07-19",
                NOW.isoformat(),
                json.dumps(
                    {
                        "kind": "shortlist",
                        "shortlist_id": "shortlist-real",
                        "as_of": "2026-07-19",
                        "entries": [{"ticker": "2331", "decision": "rejected"}],
                    }
                ),
            ),
        )
    payload = OperationPayload(
        checkpoint="final",
        artifacts=(
            {"kind": "shortlist", "ref": "shortlist-real", "selected_count": 0},
            {"kind": "shortlist", "ref": "shortlist-fake", "selected_count": 0},
        ),
        canonical_refs=("shortlist-fake",),
        completion_reason="no-shortlist-selection",
        result="none",
        next="wait",
    )

    with pytest.raises(OperationCompletionError, match="shortlist-fake"):
        service.complete(operation.operation_id, payload, completed_at=NOW)
    assert service.get(operation.operation_id).status == "active"


def test_no_shortlist_selection_rejects_an_older_zero_selection_revision(
    tmp_path: Path,
) -> None:
    db = tmp_path / "app.sqlite"
    service = OperationService(db)
    operation = service.start(
        session_kind="opportunity",
        as_of=date(2026, 7, 19),
        started_at=NOW,
        payload=_active_payload(),
    )
    with connect_rw(db) as connection:
        for shortlist_id, published_at, decision in (
            ("shortlist-zero", NOW.replace(hour=10), "rejected"),
            ("shortlist-selected", NOW.replace(hour=11), "selected"),
        ):
            connection.execute(
                """
                INSERT INTO shortlist (
                    shortlist_id, selection_id, run_revision_id, as_of, published_at, payload
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    shortlist_id,
                    f"selection-{shortlist_id}",
                    f"run-{shortlist_id}",
                    "2026-07-19",
                    published_at.isoformat(),
                    json.dumps(
                        {
                            "kind": "shortlist",
                            "shortlist_id": shortlist_id,
                            "as_of": "2026-07-19",
                            "entries": [{"ticker": "2331", "decision": decision}],
                        }
                    ),
                ),
            )
    payload = OperationPayload(
        checkpoint="final",
        artifacts=(
            {"kind": "shortlist", "ref": "shortlist-zero", "selected_count": 0},
            {"kind": "shortlist", "ref": "shortlist-selected", "selected_count": 1},
        ),
        canonical_refs=("shortlist-zero", "shortlist-selected"),
        completion_reason="no-shortlist-selection",
        result="none",
        next="wait",
    )

    with pytest.raises(OperationCompletionError, match="canonical shortlist"):
        service.complete(operation.operation_id, payload, completed_at=NOW)
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
