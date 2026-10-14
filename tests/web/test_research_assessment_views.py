"""保存提案と正確な評価pairは現在版の進行・価格で書き換わらない。"""

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from tests.engine.test_research_progression import (
    NOW,
    PUBLISHED,
    proposal_for,
    record_and_finish,
    seed_proposal,
    start,
)
from tests.helpers.assessment_v1 import (
    assessment_id,
    create_assessment_tables,
    pair_payload,
    update_review_hash,
)
from tests.helpers.proposal_v1 import create_proposal_tables

from baibai_engine.research.assessment_store import (
    latest_assessment_id,
    publish_reviewed_assessment,
)
from baibai_engine.storage.application.write import connect_rw
from baibai_web.api.server import create_app
from baibai_web.materialize import export_read_models


def _ensure_research_tables(db):
    # Fixture-only while the root coordinates the final schema. Runtime has no fallback.
    with connect_rw(db) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        if "stock_assessment" not in tables:
            create_assessment_tables(connection)
        if "investment_proposal" not in tables:
            create_proposal_tables(connection)


def seed_web_assessment(root, *, ticker="1234", number=101, as_of=None, value=1100):
    db = root / "stores/application/baibai.sqlite"
    _ensure_research_tables(db)
    as_of = as_of or NOW.date().isoformat()
    document, review = pair_payload(ticker=ticker, as_of=as_of, review_number=number + 100)
    document["valuation"]["base"]["terminal_value_per_share_yen"] = value
    review["recalculated_projections"][0]["terminal_value_per_share_yen"] = value
    update_review_hash(document, review)
    with connect_rw(db) as connection:
        connection.execute("BEGIN IMMEDIATE")
        prior = latest_assessment_id(connection, ticker)
        return publish_reviewed_assessment(
            connection,
            assessment_id(number),
            document,
            review,
            published_at=NOW if as_of == NOW.date().isoformat() else NOW + timedelta(days=1),
            supersedes_id=prior,
        )


def seed_web_proposal(
    root, *, tickers=("1234", "5678"), scope="discover_new", actions=None, complete=False
):
    db = root / "stores/application/baibai.sqlite"
    market = root / "stores/market/market.sqlite"
    ticker = tickers[0]
    pair = seed_web_assessment(root, ticker=ticker)
    service, operation = start((db, market), tickers, scope=scope)
    service.record_assessment(operation.operation_id, pair.assessment_id, now=NOW)
    for target in tickers[1:]:
        service.record_unfinished(
            operation.operation_id,
            ticker=target,
            reason="有限調査では一次資料を確認できない",
            references=("https://example.invalid/synthetic-disclosure",),
            now=NOW,
        )
    operation = service.get(operation.operation_id)
    proposal = proposal_for(operation, actions=actions)
    seed_proposal(db, proposal)
    recorded = service.record_proposal(operation.operation_id, proposal.proposal_id, now=PUBLISHED)
    if complete:
        recorded = record_and_finish(service, operation, proposal)
    return service, recorded, pair, proposal


def test_partial_proposal_preserves_scope_exact_pair_and_saved_terms_in_api_and_export(
    app_method_root, tmp_path
):
    _, operation, pair, proposal = seed_web_proposal(app_method_root)
    identity = proposal.proposal_id
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get(f"/api/investment-proposals/{identity}")
    assert response.status_code == 200
    view = response.json()
    assert view["research_scope"] == proposal.research_scope.model_dump(mode="json")
    assert view["saved_inputs"] == proposal.inputs.model_dump(mode="json")
    assert [item["action"] for item in view["items"]] == ["no_action", "defer"]
    assert view["unfinished_research"] == [
        {
            "ticker": "5678",
            "purpose": "new",
            "unfinished_reason": "有限調査では一次資料を確認できない",
            "references": ["https://example.invalid/synthetic-disclosure"],
        }
    ]
    linked = view["items"][0]
    assert linked["assessment_ref"] == proposal.items[0].assessment_ref.model_dump(mode="json")
    assert linked["assessment_review"]["review_id"] == pair.review.review_id
    assert linked["assessment_projection"]["raw"] == pair.document.model_dump(mode="json")
    assert linked["terms"] is None
    assert view["draft_sha256"] == proposal.review.draft_sha256
    assert operation.payload.human_confirmation is None

    # A newer public assessment advances only the stock's current assessment.
    newer = seed_web_assessment(
        app_method_root, number=102, as_of=(NOW + timedelta(days=1)).date().isoformat(), value=1800
    )
    db = app_method_root / "stores/application/baibai.sqlite"
    before = db.read_bytes()
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        latest = client.get("/api/securities/1234").json()["latest_assessment"]
        assert latest["revision"]["assessment_id"] == newer.assessment_id
        assert latest["document"] == newer.document.model_dump(mode="json")
        assert client.get(f"/api/investment-proposals/{identity}").json() == view
        assert client.get(f"/api/stock-assessments/{pair.assessment_id}").json()[
            "document"
        ] == pair.document.model_dump(mode="json")
    output = tmp_path / "export"
    export_read_models(app_method_root, output)
    assert json.loads((output / f"views/investment-proposal--{identity}.json").read_text()) == view
    assert (output / f"views/investment-proposal-preview--{identity}.json").is_file()
    assert db.read_bytes() == before


def test_preview_is_read_only_and_separate_from_saved_conditions(app_method_root):
    _, _, _, proposal = seed_web_proposal(
        app_method_root, tickers=("1234",), actions={"1234": "buy"}
    )
    db = app_method_root / "stores/application/baibai.sqlite"
    before = db.read_bytes()
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        saved = client.get(f"/api/investment-proposals/{proposal.proposal_id}").json()
        response = client.get(f"/api/investment-proposals/{proposal.proposal_id}/current-preview")
        assert response.status_code == 200
        preview = response.json()
        assert preview["saved_proposal_id"] == proposal.proposal_id
        assert preview["previewed_at"]
        assert preview["status"] == "blocked"
        assert preview["items"][0]["saved_terms"] == proposal.items[0].terms.model_dump(mode="json")
        assert "current_quote_calculation" in preview["items"][0]
        assert "saved_limit_revalidation" in preview["items"][0]
        assert preview["change_reasons"]
        assert client.get(f"/api/investment-proposals/{proposal.proposal_id}").json() == saved
    assert saved["items"][0]["terms"] == proposal.items[0].terms.model_dump(mode="json")
    assert db.read_bytes() == before


def test_new_routes_return_missing_ids_and_remove_old_caa_urls(app_method_root):
    _ensure_research_tables(app_method_root / "stores/application/baibai.sqlite")
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        for path in (
            "/api/stock-assessments/stock-assessment-00000000-0000-4000-8000-00000000ffff",
            "/api/investment-proposals/investment-proposal-00000000-0000-4000-8000-00000000ffff",
            "/api/investment-proposals/investment-proposal-00000000-0000-4000-8000-00000000ffff/current-preview",
            "/api/capital-allocation-assessments",
            "/api/capital-allocation-assessments/old-id",
        ):
            assert client.get(path).status_code == 404
        assert client.get("/api/stock-assessments?limit=101").status_code == 422
        assert client.get("/api/investment-proposals?limit=0").status_code == 422
        assert client.get("/api/investment-proposals?scope=invalid").status_code == 422
        invalid_cursor = client.get("/api/stock-assessments?cursor=absent")
        assert invalid_cursor.status_code == 400
        assert invalid_cursor.json()["detail"]["code"] == "invalid_cursor"


@pytest.mark.parametrize("kind", ["assessment", "proposal"])
def test_source_inconsistency_is_a_structured_error_instead_of_an_old_revision_fallback(
    app_method_root, mocker, kind
):
    _ensure_research_tables(app_method_root / "stores/application/baibai.sqlite")
    method = "assessment_detail" if kind == "assessment" else "proposal"
    mocker.patch(
        f"baibai_web.sources.db_sources.DbResearchSource.{method}",
        side_effect=ValueError("synthetic exact reference mismatch"),
    )
    path = (
        "/api/stock-assessments/invalid"
        if kind == "assessment"
        else "/api/investment-proposals/invalid"
    )
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get(path)
    assert response.status_code == 500
    assert response.json()["detail"] == {
        "code": "invalid_source",
        "message": "synthetic exact reference mismatch",
    }


def test_lists_filter_and_page_exact_publications(app_method_root):
    _, _, pair, proposal = seed_web_proposal(app_method_root)
    newer = seed_web_assessment(
        app_method_root, number=102, as_of=(NOW + timedelta(days=1)).date().isoformat()
    )
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        first = client.get("/api/stock-assessments?ticker=1234&limit=1").json()
        assert first["assessments"][0]["assessment_id"] == newer.assessment_id
        assert first["next_cursor"] == newer.assessment_id
        second = client.get(
            f"/api/stock-assessments?ticker=1234&limit=1&cursor={first['next_cursor']}"
        ).json()
        assert second["assessments"][0]["assessment_id"] == pair.assessment_id
        assert second["next_cursor"] is None
        proposals = client.get("/api/investment-proposals?scope=discover_new&ticker=5678").json()
        assert [row["proposal_id"] for row in proposals["proposals"]] == [proposal.proposal_id]
        assert (
            client.get("/api/investment-proposals?scope=review_positions").json()["proposals"] == []
        )
