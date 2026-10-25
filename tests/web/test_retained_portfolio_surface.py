"""保有・運用Webだけを維持し、旧研究UIやrunを要求しない。"""

import json
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.engine.test_position_outcome_store import _publication
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger

from baibai_engine.foundation.repository_layout import APPLICATION_DB_PATH, MARKET_DB_PATH
from baibai_engine.position.outcome_store import PortfolioOutcomeStore
from baibai_engine.tasks.service import TaskService
from baibai_web.api.server import create_app
from baibai_web.materialize import export_read_models
from baibai_web.readmodel.builders.tasks import build_tasks
from baibai_web.sources.db_sources import DbLedgerSource, DbMarketSource, DbTaskSource


def test_export_reads_only_portfolio_sources_without_recreating_legacy_stores(tmp_path):
    app = tmp_path / APPLICATION_DB_PATH
    ledger = load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    seed_ledger(app, ledger)
    task = TaskService(app).add(
        title="合成の要確認",
        kind="follow-up",
        due_date=date(2026, 10, 26),
        created_at=date(2026, 10, 25),
    )
    PortfolioOutcomeStore(app).publish(_publication())
    before = app.read_bytes()
    output = tmp_path / "export"
    paths = export_read_models(tmp_path, output)
    assert {path.name for path in paths} == {"dashboard.json", "tasks.json", "outcomes.json"}
    dashboard = json.loads((output / "views/dashboard.json").read_text())
    assert dashboard["ledger_exists"] is True
    assert dashboard["holdings"][0]["quantity"] == 200
    assert dashboard["holdings"][0]["market_value_yen"] is None
    assert dashboard["total_capital_yen"] is None
    assert dashboard["available_cash_yen"] is not None
    tasks = json.loads((output / "views/tasks.json").read_text())
    assert tasks["open_tasks"][0]["task_id"] == task.task_id
    assert tasks["earnings_calendar_available"] is False
    outcomes = json.loads((output / "views/outcomes.json").read_text())
    assert outcomes["outcomes"][0]["portfolio_twr_pct"] == 10.2
    assert outcomes["outcomes"][0]["benchmark_cumulative_return_pct"] == 8.2
    assert before == app.read_bytes()
    assert not (tmp_path / MARKET_DB_PATH).exists()
    assert not (tmp_path / "stores/screening").exists()
    assert not (tmp_path / "stores/market_context").exists()


def test_corrupt_ledger_metadata_does_not_hide_independent_tasks(tmp_path):
    app = tmp_path / "app.sqlite"
    seed_ledger(
        app, load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    )
    TaskService(app).add(
        title="要確認",
        kind="ops",
        due_date=date(2026, 10, 26),
        created_at=date(2026, 10, 25),
    )
    with closing(sqlite3.connect(app)) as connection, connection:
        connection.execute("UPDATE ledger_meta SET payload='{}' WHERE singleton=1")
    market = tmp_path / "absent-market"
    view = build_tasks(DbTaskSource(app), DbLedgerSource(app, market), DbMarketSource(market))
    assert len(view.open_tasks) == 1
    assert view.ledger_error
    assert view.upcoming_events == []


@pytest.mark.parametrize("path", ["/api/dashboard", "/api/tasks", "/api/outcomes"])
def test_local_read_surface_does_not_initialize_sources(tmp_path, path):
    with TestClient(create_app(tmp_path), base_url="http://localhost") as client:
        assert client.get(path).status_code == 200
    assert not (tmp_path / "stores").exists()


@pytest.mark.parametrize(
    "path",
    [
        "/api/screening/latest",
        "/api/market-context",
        "/api/operations",
        "/api/daily-delta",
        "/api/lake/current",
        "/api/investment-proposals",
    ],
)
def test_retired_local_api_routes_are_not_spa_fallback(tmp_path, path):
    with TestClient(create_app(tmp_path), base_url="http://localhost") as client:
        assert client.get(path).status_code == 404


def test_no_holdings_does_not_read_earnings_calendar(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("no holding should query earnings calendar")

    monkeypatch.setattr("baibai_web.sources.db_sources.read_next_earnings_dates", forbidden)
    assert (
        DbMarketSource(tmp_path / "absent").next_earnings_dates([], as_of=date(2026, 10, 25)) == {}
    )
