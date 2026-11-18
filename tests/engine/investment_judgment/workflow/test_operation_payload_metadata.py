"""Typed checkpoint notes cannot make the saved Operation unreadable."""

import json
import sqlite3
from contextlib import closing
from datetime import datetime

import pytest
from pydantic import BaseModel, ValidationError, model_serializer
from tests.helpers.db_seed import seed_historical_operation
from tests.helpers.generated_selection import seed_generated_selection
from tests.helpers.research_selection import historical_operation, selection

from baibai_engine.investment_judgment.workflow.operation import service as operation_service
from baibai_engine.investment_judgment.workflow.operation.models import OperationPayload
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import parse_selection, selection_sha256
from baibai_engine.primitives.json import encode_json
from baibai_engine.primitives.time import JST
from baibai_engine.storage.sqlite.application.write import initialize_database

NOW = datetime(2026, 7, 19, 12, tzinfo=JST)
INVALID_CASES = ("checkpoint", "result", "next", "canonical_refs", "missing_checkpoint")


def _invalid(payload, case):
    if case == "missing_checkpoint":
        raw = payload.model_dump(mode="python")
        del raw["checkpoint"]
        return OperationPayload.model_construct(**raw)
    return payload.model_copy(update={case: (None,) if case == "canonical_refs" else ""})


@pytest.mark.parametrize("case", INVALID_CASES)
@pytest.mark.parametrize("existing", [False, True])
def test_start_revalidates_notes_before_initialization(tmp_path, monkeypatch, case, existing):
    runs, app = tmp_path / "runs", tmp_path / "app"
    selected = seed_generated_selection(runs=runs, now=NOW)
    payload = OperationPayload(checkpoint="start", artifacts=(selected.model_dump(mode="json"),))
    if existing:
        initialize_database(app)
    before = app.read_bytes() if existing else None
    source = runs.read_bytes()
    monkeypatch.setattr(
        operation_service,
        "initialize_database",
        lambda _: pytest.fail("invalid metadata must not initialize or open the writer"),
    )
    with pytest.raises(ValidationError):
        OperationService(app, runs_path=runs).start(
            session_kind="capital-allocation",
            as_of=NOW.date(),
            started_at=NOW,
            payload=_invalid(payload, case),
        )
    assert (app.read_bytes() if app.exists() else None) == before
    assert runs.read_bytes() == source


@pytest.mark.parametrize("case", INVALID_CASES)
def test_checkpoint_revalidates_notes_without_changing_saved_operation(tmp_path, monkeypatch, case):
    app = tmp_path / "app"
    selected = selection(now=NOW)
    operation = historical_operation(selected)
    seed_historical_operation(app, operation)
    service = OperationService(app, runs_path=tmp_path / "absent-runs")
    before = app.read_bytes()
    monkeypatch.setattr(
        operation_service,
        "initialize_database",
        lambda _: pytest.fail("invalid metadata must not initialize or open the writer"),
    )
    with pytest.raises(ValidationError):
        service.checkpoint(operation.operation_id, _invalid(operation.payload, case), now=NOW)
    assert app.read_bytes() == before
    assert service.get(operation.operation_id) == operation
    assert service.active() == operation


@pytest.mark.parametrize("version", [2, 3, 4])
def test_valid_copied_notes_keep_saved_selection_and_retry_without_runs(tmp_path, version):
    if version == 2:
        raw = selection(now=NOW).model_dump(mode="json")
    else:
        raw = seed_generated_selection(runs=tmp_path / "runs", now=NOW).model_dump(mode="json")
        if version == 3:
            raw["schema_version"] = 3
            del raw["inputs"]["saved_company_context"]
    selected = parse_selection(raw)
    operation = historical_operation(selected)
    app = tmp_path / "app"
    seed_historical_operation(app, operation)
    service = OperationService(app, runs_path=tmp_path / "absent-runs")
    payload = operation.payload.model_copy(
        update={"checkpoint": "rechecked source", "canonical_refs": ("source-a",), "next": "review"}
    )
    updated = service.checkpoint(operation.operation_id, payload, now=NOW)
    assert updated.payload == payload
    assert selection_sha256(selection_binding(updated.payload)) == selection_sha256(selected)
    with closing(sqlite3.connect(app)) as connection:
        saved = connection.execute("SELECT payload FROM operation_session").fetchone()[0]
    assert saved == encode_json(payload.model_dump(mode="json"))
    assert json.loads(saved)["artifacts"] == [raw]
    with closing(sqlite3.connect(app)) as connection:
        before_retry = connection.execute("SELECT * FROM operation_session").fetchall()
    assert service.checkpoint(operation.operation_id, payload, now=NOW) == updated
    with closing(sqlite3.connect(app)) as connection:
        assert connection.execute("SELECT * FROM operation_session").fetchall() == before_retry
    assert service.get(operation.operation_id) == updated


def test_valid_copied_notes_start_and_complete_with_unchanged_artifacts(tmp_path):
    runs, app = tmp_path / "runs", tmp_path / "app"
    selected = seed_generated_selection(runs=runs, now=NOW)
    original = OperationPayload(checkpoint="prepare", artifacts=(selected.model_dump(mode="json"),))
    payload = original.model_copy(update={"checkpoint": "sources fixed", "next": "review"})
    service = OperationService(app, runs_path=runs)
    operation = service.start(
        session_kind="capital-allocation", as_of=NOW.date(), started_at=NOW, payload=payload
    )
    assert service.get(operation.operation_id) == operation
    assert operation.payload == payload
    completed = service.complete(
        operation.operation_id,
        result="no targets selected",
        next="wait for request",
        completed_at=NOW,
    )
    assert completed.payload == payload.model_copy(
        update={"result": "no targets selected", "next": "wait for request"}
    )
    assert service.get(operation.operation_id) == completed
    assert completed.payload.artifacts == original.artifacts


def test_metadata_validation_does_not_serialize_or_replace_artifacts():
    class MustNotSerialize(BaseModel):
        @model_serializer
        def serialize(self):
            pytest.fail("metadata validation must not walk the fixed input tree")

    raw = {"expensive_snapshot": MustNotSerialize()}
    payload = OperationPayload.model_construct(checkpoint="valid", artifacts=(raw,))
    payload.validate_metadata()
    assert payload.artifacts[0] is raw
