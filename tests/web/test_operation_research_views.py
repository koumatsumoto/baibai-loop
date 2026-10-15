"""Operationは評価/提案の参照と未完了理由だけを表示し本文を複写しない。"""

import json

import pytest
from fastapi.testclient import TestClient
from tests.engine.test_research_progression import NOW, final_payload, start
from tests.helpers.research_selection import selection
from tests.web.test_research_assessment_views import seed_web_proposal

from baibai_web.api.server import create_app
from baibai_web.materialize import export_read_models
from baibai_web.readmodel.builders.operations import build_operations_view


@pytest.mark.parametrize("scope", ["discover_new", "review_positions"])
def test_operations_preserve_exact_refs_unfinished_and_human_separation(
    app_method_root, tmp_path, scope
):
    tickers = ("2331",) if scope == "review_positions" else ("1234", "5678")
    actions = {"2331": "hold"} if scope == "review_positions" else None
    _, completed, pair, proposal = seed_web_proposal(
        app_method_root, tickers=tickers, scope=scope, actions=actions, complete=True
    )
    store = app_method_root / "stores/application/baibai.sqlite"
    before = store.read_bytes()
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get("/api/operations")
    assert response.status_code == 200
    view = response.json()["operations"][0]
    assert view["payload"] == completed.payload.model_dump(mode="json")
    assert view["payload"]["human_confirmation"] is None
    research = view["research"]
    assert research["request_scope"] == scope
    assert research["selection_summary"]
    assert research["max_deep_research_targets"] == 3
    assert research["proposal_id"] == proposal.proposal_id
    assert research["proposal_draft_sha256"] == proposal.review.draft_sha256
    target = research["targets"][0]
    assert target["selection_rationale"]
    assert target["outcome_status"] == "reviewed"
    assert target["assessment_ref"] == proposal.items[0].assessment_ref.model_dump(mode="json")
    assert target["assessment_ref"]["assessment_id"] == pair.assessment_id
    assert "holding_proposal" not in target
    assert "terms" not in target
    assert "document" not in target
    if scope == "discover_new":
        assert research["targets"][1]["outcome_status"] == "unfinished"
        assert research["targets"][1]["unfinished_reason"]
        assert research["targets"][1]["assessment_ref"] is None
    output = tmp_path / "export"
    export_read_models(app_method_root, output)
    assert json.loads((output / "views/operations.json").read_text()) == response.json()
    assert store.read_bytes() == before


def test_zero_selection_and_unrecorded_outcome_are_distinct(app_method_root, tmp_path, mocker):
    db = app_method_root / "stores/application/baibai.sqlite"
    market = app_method_root / "stores/market/market.sqlite"
    service, operation = start((db, market))
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
    assert view["research"]["proposal_id"] is None
    assert view["payload"]["human_confirmation"] is None
    output = tmp_path / "export"
    export_read_models(app_method_root, output)
    assert json.loads((output / "views/operations.json").read_text()) == response.json()
    assert db.read_bytes() == before
    source = mocker.Mock()
    source.outcomes.return_value = []
    raw = completed.public()
    raw["payload"]["artifacts"] = [
        selection(now=NOW, tickers=("2331",), scope="review_positions").model_dump(mode="json")
    ]
    source.operations.return_value = [raw]
    research = build_operations_view(source).operations[0].research
    assert research.targets[0].outcome_status is None
    assert research.targets[0].assessment_ref is None
    assert research.proposal_id is None
