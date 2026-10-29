"""保有・運用Webだけを維持し、旧研究UIやrunを要求しない。"""

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.engine.portfolio.test_position_outcome_store import _publication
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger

from baibai_engine.machine_analysis.observations.disclosures.jpx_models import (
    JPXEarningsCalendarEntry,
    JPXEarningsCalendarSnapshot,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_rebuild import (
    store_jpx_earnings_calendar_snapshot,
)
from baibai_engine.portfolio.holdings.valuation import current_portfolio
from baibai_engine.portfolio.ledger.drafts import apply_draft, build_sell_execution_draft
from baibai_engine.portfolio.ledger.store import LedgerStoreService
from baibai_engine.portfolio.performance.store import PortfolioOutcomeStore
from baibai_engine.storage.paths import APPLICATION_DB_PATH, MARKET_DB_PATH
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError
from baibai_engine.tasks.service import TaskService
from baibai_web.api.server import create_app
from baibai_web.materialize import export_read_models
from baibai_web.readmodel.builders.dashboard import build_dashboard
from baibai_web.readmodel.builders.tasks import build_tasks
from baibai_web.sources import db_sources
from baibai_web.sources.db_sources import DbLedgerSource, DbMarketSource, DbTaskSource


def test_export_reads_only_portfolio_sources_without_recreating_legacy_stores(tmp_path, mocker):
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
    old_dashboard = output / "views/dashboard.json"
    old_dashboard.parent.mkdir(parents=True)
    old_dashboard.write_text('{"legacy": true}\n')
    document_read = mocker.spy(db_sources, "load_ledger_document")
    paths = export_read_models(tmp_path, output)
    assert document_read.call_count == 2  # One document per Dashboard / Tasks view.
    assert old_dashboard.read_text() == '{"legacy": true}\n'
    assert {path.name for path in paths} == {
        "portfolio-dashboard.json",
        "tasks.json",
        "outcomes.json",
    }
    dashboard = json.loads((output / "views/portfolio-dashboard.json").read_text())
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


@pytest.mark.parametrize("store", ["absent", "empty", "unimported"])
def test_uninitialized_ledger_is_not_damage_or_zero_capital(tmp_path, store):
    app, market = tmp_path / "app.sqlite", tmp_path / "absent-market"
    if store == "empty":
        app.touch()
    elif store == "unimported":
        TaskService(app).add(
            title="台帳初期化前の用件",
            kind="ops",
            due_date=date(2026, 10, 26),
            created_at=date(2026, 10, 25),
        )
    before = app.read_bytes() if app.exists() else None
    ledger, names = DbLedgerSource(app, market), DbMarketSource(market)

    dashboard = build_dashboard(ledger, names)
    tasks = build_tasks(DbTaskSource(app), ledger, names)

    assert dashboard.ledger_exists is False
    assert dashboard.ledger_error is None
    assert dashboard.valuation_error is None
    assert dashboard.total_capital_yen is None
    assert dashboard.available_cash_yen is None
    assert dashboard.holdings_market_value_yen is None
    assert dashboard.holdings == []
    assert tasks.ledger_error is None
    assert tasks.upcoming_events == []
    assert len(tasks.open_tasks) == (1 if store == "unimported" else 0)
    assert (app.read_bytes() if app.exists() else None) == before
    assert not market.exists()


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
    dashboard = build_dashboard(DbLedgerSource(app, market), DbMarketSource(market))
    assert dashboard.ledger_exists is True
    assert dashboard.ledger_error
    assert dashboard.valuation_error is None
    assert dashboard.available_cash_yen is None
    assert dashboard.holdings == []
    assert dashboard.reservations == []


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


@pytest.mark.parametrize(
    "failed_source",
    [
        "calendar",
        "names",
        "names_date",
        "names_empty",
        "names_missing",
        "valuation_schema",
        "valuation_sql",
    ],
)
def test_optional_market_read_failure_does_not_block_independent_view_export(
    tmp_path, mocker, caplog, failed_source
):
    now = datetime.fromisoformat("2026-07-20T09:00:00+09:00")
    for module in (
        "baibai_web.sources.db_sources",
        "baibai_web.readmodel.builders.dashboard",
        "baibai_web.readmodel.builders.tasks",
    ):
        mocker.patch(f"{module}.datetime").now.return_value = now
    app, market = tmp_path / APPLICATION_DB_PATH, tmp_path / MARKET_DB_PATH
    seed_ledger(
        app, load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    )
    service = LedgerStoreService(app)
    draft = build_sell_execution_draft(
        service,
        occurred_at=datetime.fromisoformat("2026-07-12T10:00:00+09:00"),
        ticker="2331",
        quantity=100,
        price_yen=Decimal("1100"),
        decision_reference="synthetic-human-sale-note",
    )
    apply_draft(service, draft, human_confirmed=True)
    task = TaskService(app).add(
        title="確認する用件",
        kind="follow-up",
        due_date=date(2026, 7, 21),
        created_at=now.date(),
    )
    PortfolioOutcomeStore(app).publish(_publication())
    store_jpx_earnings_calendar_snapshot(
        market,
        JPXEarningsCalendarSnapshot(
            entries=(JPXEarningsCalendarEntry("2331", date(2026, 7, 21)),),
            source_urls=(),
            raw_record_count=1,
            excluded_record_count=0,
            superseded_record_count=0,
        ),
        fetched_at_utc="2026-07-20T00:00:00Z",
    )
    with closing(sqlite3.connect(market)) as connection, connection:
        if failed_source == "calendar":
            connection.execute(
                "UPDATE source_coverage SET record_count=2 WHERE source='jpx_earnings_calendar'"
            )
        elif failed_source == "names":
            connection.execute(
                "INSERT INTO jquants_master_snapshots VALUES ('2026-07-20', '23310', 'invalid')"
            )
        elif failed_source.startswith("names_"):
            record = {
                "Date": "2026-07-20",
                "Code": "23310",
                "CoName": "保有企業",
                "MktNm": "Prime",
                "S33Nm": "サービス業",
                "ProdCat": "011",
            }
            if failed_source == "names_empty":
                record["CoName"] = ""
            elif failed_source == "names_missing":
                del record["CoName"]
            connection.execute(
                "INSERT INTO jquants_master_snapshots VALUES (?, ?, ?)",
                (
                    "2026-07-0x" if failed_source == "names_date" else "2026-07-20",
                    "23310",
                    json.dumps(record),
                ),
            )
        elif failed_source == "valuation_schema":
            connection.execute("PRAGMA user_version=1")
    if failed_source == "valuation_sql":
        market.write_bytes(b"not a SQLite database")
    before = app.read_bytes(), market.read_bytes()
    valuation_failed = failed_source.startswith("valuation_")
    if valuation_failed:
        error_type = SQLiteSchemaError if failed_source == "valuation_schema" else sqlite3.Error
        with pytest.raises(error_type):
            current_portfolio(service.load(), sqlite_path=market, now=now)

    output = tmp_path / "export"
    paths = export_read_models(tmp_path, output)

    assert {path.name for path in paths} == {
        "portfolio-dashboard.json",
        "tasks.json",
        "outcomes.json",
    }
    dashboard = json.loads((output / "views/portfolio-dashboard.json").read_text())
    assert dashboard["ledger_error"] is None
    assert dashboard["holdings"][0]["quantity"] == 100
    assert dashboard["holdings"][0]["deployed_cost_yen"] == 99000
    assert dashboard["holdings"][0]["company_name"] is None
    assert dashboard["available_cash_yen"] == 10190500
    assert dashboard["reserved_cash_yen"] == 119000
    assert dashboard["deployed_cost_yen"] == 99000
    assert dashboard["realized_gross_pnl_yen"] == 6000
    assert dashboard["reservations"][0]["ticker"] == "8929"
    assert dashboard["reservations"][0]["remaining_quantity"] == 100
    assert dashboard["holdings"][0]["market_value_yen"] is None
    assert dashboard["holdings"][0]["unrealized_pnl_yen"] is None
    assert dashboard["holdings_market_value_yen"] is None
    assert dashboard["total_capital_yen"] is None
    assert dashboard["valuation_as_of"] is None
    if valuation_failed:
        expected_error = (
            "obsolete market SQLite schema"
            if failed_source == "valuation_schema"
            else "file is not a database"
        )
        assert expected_error in dashboard["valuation_error"]
    else:
        assert dashboard["valuation_error"] is None
    tasks = json.loads((output / "views/tasks.json").read_text())
    assert tasks["open_tasks"][0]["task_id"] == task.task_id
    assert tasks["ledger_error"] is None
    assert {
        "event_date": "2026-07-31",
        "kind": "reservation_expiry",
        "ticker": "8929",
        "label": "8929",
    } in tasks["upcoming_events"]
    if failed_source == "calendar" or valuation_failed:
        assert tasks["earnings_calendar_available"] is False
        assert tasks["earnings_calendar_error"]
        if failed_source == "calendar":
            assert "row count mismatch" in tasks["earnings_calendar_error"]
    else:
        assert tasks["earnings_calendar_available"] is True
        assert tasks["earnings_calendar_error"] is None
        assert tasks["upcoming_events"][0]["label"] == "2331"
        assert dashboard["holdings"][0]["company_name"] is None
        if failed_source == "names_date":
            assert "stored master snapshot has invalid date: '2026-07-0x'" in caplog.text
        elif failed_source in {"names_empty", "names_missing"}:
            assert "stored master record has invalid security fields" in caplog.text
    outcomes = json.loads((output / "views/outcomes.json").read_text())
    assert outcomes["outcomes"][0]["portfolio_twr_pct"] == 10.2
    with TestClient(create_app(tmp_path), base_url="http://localhost") as client:
        for route, expected in (
            ("dashboard", dashboard),
            ("tasks", tasks),
            ("outcomes", outcomes),
        ):
            response = client.get(f"/api/{route}")
            assert response.status_code == 200
            assert response.json() == expected
    assert (app.read_bytes(), market.read_bytes()) == before
