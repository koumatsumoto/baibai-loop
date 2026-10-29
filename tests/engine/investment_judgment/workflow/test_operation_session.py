from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from tests.helpers.db_seed import seed_historical_operation
from tests.helpers.generated_selection import seed_generated_selection
from tests.helpers.research_selection import historical_operation, selection

from baibai_engine.cli.operation import main as operation_main
from baibai_engine.investment_judgment.workflow.operation.models import (
    SESSION_KINDS,
    OperationPayload,
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


def test_operation_kinds_are_limited_to_multi_step_workflows(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert SESSION_KINDS == ("capital-allocation", "position-review")
    with pytest.raises(SystemExit):
        operation_main(["start", "--kind", "pending-result", "--as-of", "2026-07-19"], now=NOW)
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.parametrize("generated", [False, True], ids=["legacy", "generated"])
def test_complete_cli_only_adds_notes_and_preserves_saved_selection_and_artifacts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], generated: bool
) -> None:
    db = tmp_path / "app.sqlite"
    selected = (
        seed_generated_selection(runs=tmp_path / "runs.sqlite", now=NOW)
        if generated
        else selection(now=NOW)
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
                "今回の条件に合う対象なし",
                "--next",
                "新しい明示依頼を待つ",
            ],
            now=NOW,
        )
        == 0
    )
    completed = OperationService(db).get(operation.operation_id)
    assert json.loads(capsys.readouterr().out) == completed.public()
    assert completed.status == "completed"
    assert completed.payload == payload.model_copy(
        update={"result": "今回の条件に合う対象なし", "next": "新しい明示依頼を待つ"}
    )
    assert selection_sha256(selection_binding(completed.payload)) == selection_sha256(selected)
    with pytest.raises(OperationConflictError, match="immutable"):
        OperationService(db).complete(
            operation.operation_id, result="別の結果", next="別の予定", completed_at=NOW
        )
    assert OperationService(db).get(operation.operation_id) == completed


def test_operation_json_format_emits_one_machine_object(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = tmp_path / "runs.sqlite"
    selected = seed_generated_selection(runs=runs, now=NOW)
    db = tmp_path / "app.sqlite"
    draft = tmp_path / "start.yaml"
    draft.write_text(yaml.safe_dump(_active_payload(selected=selected).model_dump(mode="json")))

    assert (
        operation_main(
            [
                "--db",
                str(db),
                "--runs-path",
                str(runs),
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

    service.checkpoint(operation.operation_id, _active_payload("two", selected=selected))
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


def test_operation_start_requires_the_explicit_payload_at_the_cli_boundary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "app.sqlite"
    with pytest.raises(SystemExit) as error:
        operation_main(
            ["--db", str(db), "start", "--kind", "capital-allocation", "--as-of", "2026-07-19"],
            now=NOW,
        )
    assert error.value.code == 2
    assert "--payload" in capsys.readouterr().err
    assert not db.exists()
