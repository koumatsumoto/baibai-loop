"""保存提案の通常配信接続。欠測・破損を既存viewへ波及させない。"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.helpers.proposal import PUBLISHED, backend_case, reviewed_proposal

from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.storage.paths import APPLICATION_DB_PATH, MARKET_DB_PATH
from baibai_engine.storage.sqlite.application.write import initialize_database
from baibai_web.api.server import create_app
from baibai_web.contracts_export import ROOT_VIEWS
from baibai_web.materialize import export_read_models


def test_materialized_route_keys_match_the_generated_root_contract():
    routes = json.loads(Path("web/contracts/routes.json").read_text())["routes"]
    assert set(routes.values()) == set(ROOT_VIEWS)
    assert routes["/api/proposal/latest"] == "views/latest-proposal.json"


@pytest.mark.parametrize("state", ["empty", "missing", "broken_schema"])
def test_proposal_empty_or_unavailable_does_not_stop_other_views(tmp_path, state):
    app = tmp_path / APPLICATION_DB_PATH
    if state != "missing":
        initialize_database(app)
        if state == "broken_schema":
            with closing(sqlite3.connect(app)) as connection, connection:
                connection.execute("DROP TABLE investment_proposal")
    before = app.read_bytes() if app.exists() else None
    output = tmp_path / "export"
    paths = export_read_models(tmp_path, output)
    assert {f"views/{path.name}" for path in paths} == set(ROOT_VIEWS)
    view = json.loads((output / "views/latest-proposal.json").read_text())
    expected = "empty" if state == "empty" else "unavailable"
    assert view["status"] == expected
    assert view["proposal"] is None
    with TestClient(create_app(tmp_path), base_url="http://localhost") as client:
        for path in ("/api/dashboard", "/api/tasks", "/api/outcomes", "/api/proposal/latest"):
            response = client.get(path)
            assert response.status_code == 200
            if path == "/api/proposal/latest":
                assert response.json()["status"] == expected
        assert client.post("/api/proposal/latest").status_code == 405
    assert (app.read_bytes() if app.exists() else None) == before
    assert not (tmp_path / MARKET_DB_PATH).exists()


def test_export_replaces_old_ready_with_unavailable_when_latest_is_corrupt(tmp_path):
    case = backend_case(tmp_path)
    saved = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(reviewed_proposal(case))
    app = tmp_path / APPLICATION_DB_PATH
    app.parent.mkdir(parents=True, exist_ok=True)
    case.app.rename(app)
    output = tmp_path / "export"
    export_read_models(tmp_path, output)
    proposal_path = output / "views/latest-proposal.json"
    ready = json.loads(proposal_path.read_text())
    assert ready["status"] == "ready"
    assert ready["proposal"]["proposal_id"] == saved.proposal_id
    assert ready["proposal"]["items"][0]["assessment"]["assessment_id"] == case.pair.assessment_id
    with TestClient(create_app(tmp_path), base_url="http://localhost") as client:
        response = client.get("/api/proposal/latest")
        assert response.status_code == 200
        assert response.json()["proposal"] == ready["proposal"]
    with closing(sqlite3.connect(app)) as connection, connection:
        connection.execute("DROP TRIGGER investment_proposal_immutable")
        connection.execute(
            "UPDATE investment_proposal SET draft_sha256=? WHERE proposal_id=?",
            ("0" * 64, saved.proposal_id),
        )
    before = app.read_bytes()
    paths = export_read_models(tmp_path, output)
    assert len(paths) == 4
    damaged = json.loads(proposal_path.read_text())
    assert damaged["status"] == "unavailable"
    assert damaged["proposal"] is None
    assert damaged["error"] == "保存済み提案を読み取れません。"
    assert str(app) not in proposal_path.read_text()
    dashboard = json.loads((output / "views/portfolio-dashboard.json").read_text())
    assert dashboard["ledger_exists"] is True
    assert dashboard["ledger_error"] is None
    assert json.loads((output / "views/tasks.json").read_text())["ledger_error"] is None
    assert json.loads((output / "views/outcomes.json").read_text())["outcomes"] == []
    assert app.read_bytes() == before
    assert not (tmp_path / MARKET_DB_PATH).exists()
