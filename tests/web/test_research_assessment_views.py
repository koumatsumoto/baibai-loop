"""現行writerの部分完了CAAをWeb・exportでも同じ範囲として読む。"""

import json
from datetime import timedelta

from fastapi.testclient import TestClient
from tests.engine.test_research_progression import NOW, assessment, seed_research, start
from tests.helpers.research_v4 import pair_payload

from baibai_engine.read_api.capital_allocation import list_capital_allocation_assessment_payloads
from baibai_engine.research.capital_allocation_service import CapitalAllocationAssessmentService
from baibai_engine.research.thesis import ThesisDocument, thesis_core_hash
from baibai_engine.research.thesis_store import ThesisStoreService
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
    bound = view["alternatives"][0]
    assert bound["thesis_id"] == "thesis-1234"
    assert bound["thesis_review"]["review_id"] == bound["thesis_review_id"] == "review-1234"
    assert (
        bound["thesis_projection"]["raw"]["investment_case"]["explanation"]
        == "受注回復が価格へ未反映"
    )
    assert bound["pmax_raw_yen"] == 1101.7857142857142

    # A normal publication advances the company page; the CAA remains bound to its old pair.
    next_date = (NOW + timedelta(days=1)).date().isoformat()
    thesis, review = pair_payload(ticker="1234", as_of=next_date, quote_as_of=next_date)
    thesis["valuation"]["base"]["terminal_value_per_share_yen"] = 1804
    thesis["valuation"]["base"]["calculation"] = "synthetic revised terminal value 1804 yen"
    thesis["investment_case"]["explanation"] = "改訂された企業評価"
    review["review_id"] = "review-1234-next"
    review["reviewed_thesis_sha256"] = thesis_core_hash(ThesisDocument.model_validate(thesis))
    review["recalculated_projections"][0]["terminal_value_per_share_yen"] = 1804
    ThesisStoreService(db, clock=lambda: NOW + timedelta(days=1)).publish_reviewed_thesis(
        "thesis-1234-next", thesis, review, supersedes_id="thesis-1234"
    )
    before = db.read_bytes()
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        latest = client.get("/api/securities/1234").json()["latest_thesis"]
        assert latest["revision"]["thesis_id"] == "thesis-1234-next"
        assert latest["revision"]["pmax_raw_yen"] == 1637.5
        assert client.get(f"/api/capital-allocation-assessments/{identity}").json() == view
    out = tmp_path / "export"
    export_read_models(app_method_root, out)
    saved = json.loads((out / f"views/capital-allocation-assessment--{identity}.json").read_text())
    assert saved == view
    assert db.read_bytes() == before
