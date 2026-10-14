from __future__ import annotations

import json
import shutil

import pytest
from fastapi.testclient import TestClient
from tests.engine.test_position_review_v3 import holding_case as holding_case
from tests.engine.test_research_progression import (
    NOW,
    final_payload,
    seed_research,
    start,
)

from baibai_engine.research.operation.progress import progress_binding
from baibai_engine.research.position_review import RemainingReward
from baibai_engine.research.position_review_service import PositionReviewService
from baibai_web.api.server import create_app
from baibai_web.materialize import export_read_models
from baibai_web.readmodel.builders.operations import build_operations_view


@pytest.mark.parametrize(("reward", "action"), [("sufficient", "hold"), ("uncertain", None)])
def test_operations_preserve_new_holding_unfinished_and_unconfirmed_proposals(
    app_method_root, tmp_path, holding_case, reward, action
):
    db, market, _ = holding_case
    seed_research(db)
    service, operation = start(db, market, (("2331", "holding"),))
    draft = PositionReviewService(db, sqlite_path=market, clock=lambda: NOW).build(
        thesis_id="current", position_id="2331"
    )
    draft = draft.model_copy(
        update={
            "remaining_reward": RemainingReward(status=reward, reason="synthetic saved judgment")
        }
    )
    captured = service.record_position_proposal(operation.operation_id, draft, now=NOW)
    holding = progress_binding(captured.payload).outcomes[0]
    completed = service.complete(operation.operation_id, final_payload(captured), completed_at=NOW)
    store = app_method_root / "stores/application/baibai.sqlite"
    shutil.copyfile(db, store)
    before = store.read_bytes()
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get("/api/operations")
    assert response.status_code == 200
    view = response.json()["operations"][0]
    assert view["payload"] == completed.payload.model_dump(mode="json")
    assert view["payload"]["human_confirmation"] is None
    research = view["research"]
    assert research["request_scope"] == "review_positions"
    assert research["selection_summary"]
    assert research["max_deep_research_targets"] == 3
    assert research["capital_allocation_assessment_id"] is None
    proposal = research["targets"][0]
    assert proposal["selection_rationale"]
    assert (proposal["ticker"], proposal["purpose"], proposal["outcome_status"]) == (
        "2331",
        "holding",
        "proposal",
    )
    assert proposal["holding_proposal"] == holding.draft.model_dump(mode="json")
    assert proposal["holding_proposal_null_reason"] == (
        "残存見返りの判断が未確認です" if action is None else None
    )
    assert proposal["holding_proposal"]["action"] == action
    assert proposal["checked_at"] == NOW.isoformat()
    output = tmp_path / "export"
    export_read_models(app_method_root, output)
    assert json.loads((output / "views/operations.json").read_text()) == response.json()
    assert store.read_bytes() == before


def test_zero_selection_is_distinct_from_absent_selection_and_unrecorded_outcome(
    app_method_root, tmp_path, mocker
):
    db = app_method_root / "stores/application/baibai.sqlite"
    market = app_method_root / "stores/market/market.sqlite"
    service, operation = start(db, market, ())
    completed = service.complete(operation.operation_id, final_payload(operation), completed_at=NOW)
    before = db.read_bytes()
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get("/api/operations")
    view = next(
        item
        for item in response.json()["operations"]
        if item["operation_id"] == operation.operation_id
    )
    assert view["research"]["targets"] == []
    assert view["research"]["capital_allocation_assessment_id"] is None
    assert view["payload"]["human_confirmation"] is None
    output = tmp_path / "export"
    export_read_models(app_method_root, output)
    assert json.loads((output / "views/operations.json").read_text()) == response.json()
    assert db.read_bytes() == before

    source = mocker.Mock()
    source.outcomes.return_value = []
    raw = completed.public()
    from tests.helpers.research_selection import selection

    raw["payload"]["artifacts"] = [
        selection(now=NOW, tickers=("2331",), scope="review_positions").model_dump(mode="json")
    ]
    source.operations.return_value = [raw]
    research = build_operations_view(source).operations[0].research
    assert research.targets[0].outcome_status is None
    assert research.targets[0].holding_proposal is None
