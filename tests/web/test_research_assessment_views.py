"""現行writerの部分完了CAAをWeb・exportでも同じ範囲として読む。"""

import json

from fastapi.testclient import TestClient
from tests.engine.test_research_progression import NOW, assessment, seed_research, start

from baibai_engine.read_api.capital_allocation import list_capital_allocation_assessment_payloads
from baibai_engine.research.capital_allocation_service import CapitalAllocationAssessmentService
from baibai_web.api.server import create_app
from baibai_web.materialize import export_read_models


def test_current_partial_assessment_preserves_scope_in_api_and_export(app_method_root, tmp_path):
    db = app_method_root / "stores/application/baibai.sqlite"
    market = app_method_root / "stores/market/market.sqlite"
    seed_research(db)
    service, operation = start(db, market, (("1234", "new"), ("5678", "new")))
    service.record_thesis(operation.operation_id, "thesis-1234", now=NOW)
    service.record_unfinished(
        operation.operation_id,
        ticker="5678",
        reason="synthetic finite investigation ended",
        references=("https://example.invalid/disclosure",),
        now=NOW,
    )
    published = CapitalAllocationAssessmentService(db, clock=lambda: NOW).publish(
        assessment(db, ["thesis-1234"])
    )
    identity = published.capital_allocation_assessment_id
    before = db.read_bytes()
    indexed = list_capital_allocation_assessment_payloads(db)[0]
    assert indexed == published.model_dump(mode="json")
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get(f"/api/capital-allocation-assessments/{identity}")
    assert response.status_code == 200
    view = response.json()
    assert view["research_operation_id"] == operation.operation_id
    assert view["selected_new_target_count"] == 2
    assert [item["ticker"] for item in view["alternatives"]] == ["1234"]
    assert view["unfinished_research"] == [
        {
            "ticker": "5678",
            "reason": "synthetic finite investigation ended",
            "references": ["https://example.invalid/disclosure"],
        }
    ]
    out = tmp_path / "export"
    export_read_models(app_method_root, out)
    saved = json.loads((out / f"views/capital-allocation-assessment--{identity}.json").read_text())
    assert saved == view
    assert db.read_bytes() == before
