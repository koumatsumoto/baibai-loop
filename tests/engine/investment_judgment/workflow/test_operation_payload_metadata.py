"""Typed checkpoint notes cannot make the saved Operation unreadable."""

import json
import sqlite3
from contextlib import closing
from datetime import datetime

import pytest
from pydantic import BaseModel, model_serializer
from tests.helpers.db_seed import seed_historical_operation
from tests.helpers.generated_selection import seed_generated_selection
from tests.helpers.research_selection import historical_operation, selection

from baibai_engine.investment_judgment.workflow.operation.models import OperationPayload
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import parse_selection, selection_sha256
from baibai_engine.primitives.json import encode_json
from baibai_engine.primitives.time import JST

NOW = datetime(2026, 7, 19, 12, tzinfo=JST)


@pytest.mark.parametrize(("note", "next_action"), [("", None), (" ", None), ("note", " ")])
def test_checkpoint_rejects_blank_notes_without_changing_saved_operation(
    tmp_path, note, next_action
):
    app = tmp_path / "app"
    operation = historical_operation(selection(now=NOW))
    seed_historical_operation(app, operation)
    before = app.read_bytes()
    with pytest.raises(ValueError, match="blank"):
        OperationService(app).checkpoint(
            operation.operation_id, note=note, next=next_action, now=NOW
        )
    assert app.read_bytes() == before


@pytest.mark.parametrize("version", [2, 3, 4, 5])
def test_valid_copied_notes_keep_saved_selection_and_retry_without_runs(tmp_path, version):
    if version == 2:
        raw = selection(now=NOW).model_dump(mode="json")
    else:
        from tests.helpers.generated_selection import legacy_selection_payload

        source = seed_generated_selection(runs=tmp_path / "runs", now=NOW)
        raw = (
            source.model_dump(mode="json")
            if version == 5
            else legacy_selection_payload(source, tmp_path / "runs", version=version)
        )
    selected = parse_selection(raw)
    operation = historical_operation(selected)
    app = tmp_path / "app"
    seed_historical_operation(app, operation)
    service = OperationService(app, runs_path=tmp_path / "absent-runs")
    payload = operation.payload.model_copy(
        update={"checkpoint": "rechecked source", "next": "review"}
    )
    updated = service.checkpoint(
        operation.operation_id, note=payload.checkpoint, next=payload.next, now=NOW
    )
    assert updated.payload == payload
    assert selection_sha256(selection_binding(updated.payload)) == selection_sha256(selected)
    with closing(sqlite3.connect(app)) as connection:
        saved = connection.execute("SELECT payload FROM operation_session").fetchone()[0]
    assert saved == encode_json(payload.model_dump(mode="json"))
    assert json.loads(saved)["artifacts"] == [raw]
    with closing(sqlite3.connect(app)) as connection:
        before_retry = connection.execute("SELECT * FROM operation_session").fetchall()
    assert (
        service.checkpoint(
            operation.operation_id, note=payload.checkpoint, next=payload.next, now=NOW
        )
        == updated
    )
    with closing(sqlite3.connect(app)) as connection:
        assert connection.execute("SELECT * FROM operation_session").fetchall() == before_retry
    assert service.get(operation.operation_id) == updated


def test_metadata_validation_does_not_serialize_or_replace_artifacts():
    class MustNotSerialize(BaseModel):
        @model_serializer
        def serialize(self):
            pytest.fail("metadata validation must not walk the fixed input tree")

    raw = {"expensive_snapshot": MustNotSerialize()}
    payload = OperationPayload.model_construct(checkpoint="valid", artifacts=(raw,))
    payload.validate_metadata()
    assert payload.artifacts[0] is raw
