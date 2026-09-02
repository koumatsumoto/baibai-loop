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
    values["human_confirmation"] = {
        "request": "confirm",
        "result": "human confirmed",
    }
    values["artifacts"] = ({"kind": "review", "summary": "reviewed"},)
    if kind == "position-review":
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
        session_kind="capital-allocation",
        as_of=date(2026, 7, 19),
        started_at=NOW,
        payload=_active_payload("one"),
    )

    with pytest.raises(OperationConflictError, match="active operation already exists"):
        service.start(
            session_kind="position-review",
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


def test_capital_allocation_with_no_research_triage_selection_completes_without_human_confirmation(
    tmp_path: Path,
) -> None:
    service = OperationService(tmp_path / "app.sqlite")
    operation = service.start(
        session_kind="capital-allocation",
        as_of=date(2026, 7, 19),
        started_at=NOW,
        payload=_active_payload(),
    )
    with connect_rw(tmp_path / "app.sqlite") as connection:
        connection.execute(
            """
            INSERT INTO research_triage (
                research_triage_id, review_set_id, run_revision_id, as_of, published_at, payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "research_triage-1",
                "review-set-1",
                "run-1",
                "2026-07-19",
                NOW.isoformat(),
                json.dumps(
                    {
                        "kind": "research_triage",
                        "research_triage_id": "research_triage-1",
                        "as_of": "2026-07-19",
                        "entries": [{"ticker": "2331", "decision": "skip"}],
                    }
                ),
            ),
        )
    payload = OperationPayload(
        checkpoint="research_triage cycle complete",
        artifacts=({"kind": "research_triage", "ref": "research_triage-1", "research_count": 0},),
        canonical_refs=("research_triage-1",),
        completion_reason="no-research",
        result="no candidate was admitted to the Research Set",
        next="wait for the next capital-allocation trigger",
    )

    completed = service.complete(operation.operation_id, payload, completed_at=NOW)

    assert completed.status == "completed"
    assert completed.payload.human_confirmation is None
    assert completed.payload.completion_reason == "no-research"


@pytest.mark.parametrize(
    "payload",
    [
        OperationPayload(
            checkpoint="final",
            artifacts=(
                {"kind": "research_triage", "ref": "research_triage-1", "research_count": 1},
            ),
            canonical_refs=("research_triage-1",),
            completion_reason="no-research",
            result="done",
            next="wait",
        ),
        OperationPayload(
            checkpoint="final",
            artifacts=(
                {"kind": "research_triage", "ref": "research_triage-1", "research_count": 0},
            ),
            canonical_refs=("research_triage-1",),
            human_confirmation={"request": "confirm", "result": "not applicable"},
            completion_reason="no-research",
            result="done",
            next="wait",
        ),
    ],
)
def test_no_research_triage_selection_completion_rejects_false_evidence(
    tmp_path: Path,
    payload: OperationPayload,
) -> None:
    service = OperationService(tmp_path / "app.sqlite")
    operation = service.start(
        session_kind="capital-allocation",
        as_of=date(2026, 7, 19),
        started_at=NOW,
        payload=_active_payload(),
    )

    with pytest.raises(OperationCompletionError):
        service.complete(operation.operation_id, payload, completed_at=NOW)


def test_no_research_triage_selection_reason_is_capital_allocation_only(tmp_path: Path) -> None:
    service = OperationService(tmp_path / "app.sqlite")
    operation = service.start(
        session_kind="position-review",
        as_of=date(2026, 7, 19),
        started_at=NOW,
        payload=_active_payload(),
    )
    payload = OperationPayload(
        checkpoint="final",
        artifacts=({"kind": "research_triage", "research_count": 0},),
        canonical_refs=("research_triage-1",),
        completion_reason="no-research",
        result="done",
        next="wait",
    )

    with pytest.raises(OperationCompletionError, match="completion_reason"):
        service.complete(operation.operation_id, payload, completed_at=NOW)


@pytest.mark.parametrize(
    ("research_triage_as_of", "decision", "message"),
    [
        (None, None, "canonical ResearchTriage"),
        ("2026-07-18", "skip", "canonical ResearchTriage"),
        ("2026-07-19", "research", "zero research entries"),
    ],
)
def test_no_research_triage_selection_completion_checks_the_canonical_publication(
    tmp_path: Path,
    research_triage_as_of: str | None,
    decision: str | None,
    message: str,
) -> None:
    db = tmp_path / "app.sqlite"
    service = OperationService(db)
    operation = service.start(
        session_kind="capital-allocation",
        as_of=date(2026, 7, 19),
        started_at=NOW,
        payload=_active_payload(),
    )
    if research_triage_as_of is not None and decision is not None:
        with connect_rw(db) as connection:
            connection.execute(
                """
                INSERT INTO research_triage (
                    research_triage_id, review_set_id, run_revision_id, as_of, published_at, payload
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "research_triage-1",
                    "review-set-1",
                    "run-1",
                    research_triage_as_of,
                    NOW.isoformat(),
                    json.dumps(
                        {
                            "kind": "research_triage",
                            "research_triage_id": "research_triage-1",
                            "as_of": research_triage_as_of,
                            "entries": [{"ticker": "2331", "decision": decision}],
                        }
                    ),
                ),
            )
    payload = OperationPayload(
        checkpoint="final",
        artifacts=({"kind": "research_triage", "ref": "research_triage-1", "research_count": 0},),
        canonical_refs=("research_triage-1",),
        completion_reason="no-research",
        result="none",
        next="wait",
    )

    with pytest.raises(OperationCompletionError, match=message):
        service.complete(operation.operation_id, payload, completed_at=NOW)
    assert service.get(operation.operation_id).status == "active"


def test_no_research_triage_selection_checks_the_canonical_artifact_reference(
    tmp_path: Path,
) -> None:
    db = tmp_path / "app.sqlite"
    service = OperationService(db)
    operation = service.start(
        session_kind="capital-allocation",
        as_of=date(2026, 7, 19),
        started_at=NOW,
        payload=_active_payload(),
    )
    with connect_rw(db) as connection:
        connection.execute(
            """
            INSERT INTO research_triage (
                research_triage_id, review_set_id, run_revision_id, as_of, published_at, payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "research_triage-real",
                "review-set-1",
                "run-1",
                "2026-07-19",
                NOW.isoformat(),
                json.dumps(
                    {
                        "kind": "research_triage",
                        "research_triage_id": "research_triage-real",
                        "as_of": "2026-07-19",
                        "entries": [{"ticker": "2331", "decision": "skip"}],
                    }
                ),
            ),
        )
    payload = OperationPayload(
        checkpoint="final",
        artifacts=(
            {"kind": "research_triage", "ref": "research_triage-real", "research_count": 0},
            {"kind": "research_triage", "ref": "research_triage-fake", "research_count": 0},
        ),
        canonical_refs=("research_triage-fake",),
        completion_reason="no-research",
        result="none",
        next="wait",
    )

    with pytest.raises(OperationCompletionError, match="research_triage-fake"):
        service.complete(operation.operation_id, payload, completed_at=NOW)
    assert service.get(operation.operation_id).status == "active"


def test_no_research_triage_selection_rejects_an_older_zero_selection_revision(
    tmp_path: Path,
) -> None:
    db = tmp_path / "app.sqlite"
    service = OperationService(db)
    operation = service.start(
        session_kind="capital-allocation",
        as_of=date(2026, 7, 19),
        started_at=NOW,
        payload=_active_payload(),
    )
    with connect_rw(db) as connection:
        for research_triage_id, published_at, decision in (
            ("research_triage-zero", NOW.replace(hour=10), "skip"),
            ("research_triage-selected", NOW.replace(hour=11), "research"),
        ):
            connection.execute(
                """
                INSERT INTO research_triage (
                    research_triage_id, review_set_id, run_revision_id, as_of, published_at, payload
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    research_triage_id,
                    f"review-set-{research_triage_id}",
                    f"run-{research_triage_id}",
                    "2026-07-19",
                    published_at.isoformat(),
                    json.dumps(
                        {
                            "kind": "research_triage",
                            "research_triage_id": research_triage_id,
                            "as_of": "2026-07-19",
                            "entries": [{"ticker": "2331", "decision": decision}],
                        }
                    ),
                ),
            )
    payload = OperationPayload(
        checkpoint="final",
        artifacts=(
            {"kind": "research_triage", "ref": "research_triage-zero", "research_count": 0},
            {"kind": "research_triage", "ref": "research_triage-selected", "research_count": 1},
        ),
        canonical_refs=("research_triage-zero", "research_triage-selected"),
        completion_reason="no-research",
        result="none",
        next="wait",
    )

    with pytest.raises(OperationCompletionError, match="canonical ResearchTriage"):
        service.complete(operation.operation_id, payload, completed_at=NOW)
    assert service.get(operation.operation_id).status == "active"


def test_completed_row_is_immutable_through_service_and_database(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    service = OperationService(db)
    operation = service.start(
        session_kind="capital-allocation",
        as_of=date(2026, 7, 19),
        started_at=NOW,
        payload=_active_payload(),
    )
    service.complete(
        operation.operation_id, _complete_payload("capital-allocation"), completed_at=NOW
    )

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
                "position-review",
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
        yaml.safe_dump(_complete_payload("position-review").model_dump(mode="json")),
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
