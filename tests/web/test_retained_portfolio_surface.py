"""保有・運用Webを維持し、旧研究UIやrunを要求しない。"""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger
from tests.helpers.outcome_publication import _publication

from baibai_engine.machine_analysis.observations.disclosures.jpx_models import (
    JPXEarningsCalendarEntry,
    JPXEarningsCalendarSnapshot,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_rebuild import (
    store_jpx_earnings_calendar_snapshot,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master import (
    JapanListingAttributes,
    ListingMasterRow,
    ListingSlice,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    CurrentListingMarket,
)
from baibai_engine.portfolio.holdings.valuation import current_portfolio
from baibai_engine.portfolio.ledger.drafts import apply_draft, build_sell_execution_draft
from baibai_engine.portfolio.ledger.store import LedgerStoreService
from baibai_engine.portfolio.performance.store import PortfolioOutcomeStore
from baibai_engine.storage.paths import APPLICATION_DB_PATH, MARKET_DB_PATH
from baibai_engine.storage.sqlite.application.schema import APPLICATION_SCHEMA_VERSION
from baibai_engine.storage.sqlite.application.write import initialize_database
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError
from baibai_web.materialize import export_read_models
from baibai_web.readmodel.builders.dashboard import build_dashboard
from baibai_web.sources import db_sources
from baibai_web.sources.db_sources import CurrentJpMarketSource, DbLedgerSource


def test_export_reads_retained_sources_without_recreating_legacy_stores(tmp_path, mocker):
    app = tmp_path / APPLICATION_DB_PATH
    ledger = load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    seed_ledger(app, ledger)
    PortfolioOutcomeStore(app).publish(_publication())
    before = app.read_bytes()
    output = tmp_path / "export"
    old_dashboard = output / "views/dashboard.json"
    old_dashboard.parent.mkdir(parents=True)
    old_dashboard.write_text('{"legacy": true}\n')
    document_read = mocker.spy(db_sources, "load_ledger_document")
    paths = export_read_models(tmp_path, output)
    assert document_read.call_count == 1  # Dashboard is the only ledger view.
    assert old_dashboard.read_text() == '{"legacy": true}\n'
    assert {path.name for path in paths} == {
        "portfolio-dashboard.json",
        "outcomes.json",
        "latest-proposal.json",
        "proposal-history.json",
        "latest-us-proposal.json",
    }
    dashboard = json.loads((output / "views/portfolio-dashboard.json").read_text())
    assert dashboard["ledger_exists"] is True
    assert dashboard["holdings"][0]["quantity"] == 200
    assert dashboard["holdings"][0]["market_value_yen"] is None
    assert dashboard["total_capital_yen"] is None
    assert dashboard["available_cash_yen"] is not None
    outcomes = json.loads((output / "views/outcomes.json").read_text())
    assert outcomes["outcomes"][0]["portfolio_twr_pct"] == 10.2
    assert outcomes["outcomes"][0]["benchmark_cumulative_return_pct"] == 8.2
    assert before == app.read_bytes()
    assert not (tmp_path / MARKET_DB_PATH).exists()
    assert not (tmp_path / "stores/screening").exists()
    assert not (tmp_path / "stores/market_context").exists()


def test_retired_task_data_remains_untouched_and_cannot_block_portfolio_views(tmp_path):
    app = tmp_path / APPLICATION_DB_PATH
    ledger = load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    seed_ledger(app, ledger)
    PortfolioOutcomeStore(app).publish(_publication())
    # An upgraded store can still contain private, invalid Task payloads.
    # Retiring the feature neither parses them nor runs a destructive migration.
    with closing(sqlite3.connect(app)) as connection, connection:
        connection.executescript("""
            CREATE TABLE task (
                task_id TEXT PRIMARY KEY,
                status TEXT NOT NULL CHECK (status IN ('open', 'done', 'dropped')),
                kind TEXT NOT NULL CHECK (kind IN ('earnings-review', 'ops', 'follow-up', 'other')),
                ticker TEXT, due_date TEXT NOT NULL, event_date TEXT,
                created_at TEXT NOT NULL, closed_at TEXT,
                payload TEXT NOT NULL CHECK (json_valid(payload))
            ) STRICT;
            CREATE INDEX task_status_due_idx ON task(status, due_date, task_id);
            CREATE INDEX task_ticker_idx ON task(ticker) WHERE ticker IS NOT NULL;
        """)
        connection.execute(
            "INSERT INTO task VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "task-20261025-retired",
                "open",
                "ops",
                None,
                "2026-10-26",
                None,
                "2026-10-25",
                None,
                '{"private_note":"SYNTHETIC_RETIRED_TASK"}',
            ),
        )
    before = app.read_bytes()
    assert initialize_database(app) == APPLICATION_SCHEMA_VERSION
    output = tmp_path / "export"
    old_view = output / "views/tasks.json"
    old_view.parent.mkdir(parents=True)
    old_view.write_text('{"retired_private_view":true}\n')

    paths = export_read_models(tmp_path, output)

    assert {path.name for path in paths} == {
        "portfolio-dashboard.json",
        "outcomes.json",
        "latest-proposal.json",
        "proposal-history.json",
        "latest-us-proposal.json",
    }
    assert old_view.read_text() == '{"retired_private_view":true}\n'
    assert all("SYNTHETIC_RETIRED_TASK" not in path.read_text() for path in paths)
    dashboard = json.loads((output / "views/portfolio-dashboard.json").read_text())
    assert dashboard["holdings"]
    assert dashboard["ledger_error"] is None
    assert app.read_bytes() == before


@pytest.mark.parametrize("store", ["absent", "empty", "unimported"])
def test_uninitialized_ledger_is_not_damage_or_zero_capital(tmp_path, store):
    app, market = tmp_path / "app.sqlite", tmp_path / "absent-market"
    if store == "empty":
        app.touch()
    elif store == "unimported":
        initialize_database(app)
    before = app.read_bytes() if app.exists() else None
    ledger, names = DbLedgerSource(app, market), CurrentJpMarketSource(market)

    dashboard = build_dashboard(ledger, names)

    assert dashboard.ledger_exists is False
    assert dashboard.ledger_error is None
    assert dashboard.valuation_error is None
    assert dashboard.total_capital_yen is None
    assert dashboard.available_cash_yen is None
    assert dashboard.holdings_market_value_yen is None
    assert dashboard.holdings == []
    assert (app.read_bytes() if app.exists() else None) == before
    assert not market.exists()


def test_corrupt_ledger_metadata_is_not_presented_as_empty(tmp_path):
    app = tmp_path / "app.sqlite"
    seed_ledger(
        app, load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    )
    with closing(sqlite3.connect(app)) as connection, connection:
        connection.execute("UPDATE ledger_meta SET payload='{}' WHERE singleton=1")
    market = tmp_path / "absent-market"
    dashboard = build_dashboard(DbLedgerSource(app, market), CurrentJpMarketSource(market))
    assert dashboard.ledger_exists is True
    assert dashboard.ledger_error
    assert dashboard.valuation_error is None
    assert dashboard.available_cash_yen is None
    assert dashboard.holdings == []
    assert dashboard.reservations == []


@pytest.mark.parametrize(
    ("boundary", "error_type"),
    [
        ("ledger", ValueError),
        ("ledger", RuntimeError),
        ("ledger", sqlite3.OperationalError),
        ("valuation", SQLiteSchemaError),
        ("valuation", sqlite3.OperationalError),
        ("valuation", ValueError),
    ],
)
def test_dashboard_read_errors_do_not_publish_internal_diagnostics(
    tmp_path, mocker, boundary, error_type
):
    app, market = tmp_path / APPLICATION_DB_PATH, tmp_path / MARKET_DB_PATH
    seed_ledger(
        app, load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    )
    before = app.read_bytes()
    diagnostic = "SYNTHETIC_PRIVATE_NOTE /private/SYNTHETIC_PATH SELECT SYNTHETIC_SQL"
    target = "load_ledger_document" if boundary == "ledger" else "current_portfolio"
    mocker.patch.object(db_sources, target, side_effect=error_type(diagnostic))

    paths = export_read_models(tmp_path, tmp_path / "export")
    exported = next(path for path in paths if path.name == "portfolio-dashboard.json")
    dashboard = json.loads(exported.read_text())
    assert dashboard["ledger_exists"] is True
    assert dashboard["ledger_error"] == (
        "保存済み台帳を読み取れません。" if boundary == "ledger" else None
    )
    assert dashboard["valuation_error"] == (
        "市場評価を読み取れません。" if boundary == "valuation" else None
    )
    assert dashboard["holding_quantity_unresolved"] is False
    assert dashboard["total_capital_yen"] is None
    if boundary == "valuation":
        assert dashboard["available_cash_yen"] is not None
        assert dashboard["holdings"][0]["quantity"] == 200
        assert dashboard["reservations"]
    else:
        assert dashboard["available_cash_yen"] is None
        assert dashboard["holdings"] == dashboard["reservations"] == []
    assert "SYNTHETIC_" not in json.dumps(dashboard)
    assert app.read_bytes() == before
    assert not market.exists()


def test_invalid_saved_ledger_payload_does_not_escape_through_dashboard(tmp_path):
    app, market = tmp_path / APPLICATION_DB_PATH, tmp_path / MARKET_DB_PATH
    seed_ledger(
        app, load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    )
    with closing(sqlite3.connect(app)) as connection, connection:
        connection.execute(
            "UPDATE ledger_meta SET payload=? WHERE singleton=1",
            (json.dumps({"as_of": "SYNTHETIC_PRIVATE_DATE", "note": "SYNTHETIC_PRIVATE_NOTE"}),),
        )
    before = app.read_bytes()

    paths = export_read_models(tmp_path, tmp_path / "export")
    exported = next(path for path in paths if path.name == "portfolio-dashboard.json")
    dashboard = json.loads(exported.read_text())
    assert dashboard["ledger_exists"] is True
    assert dashboard["ledger_error"] == "保存済み台帳を読み取れません。"
    assert dashboard["available_cash_yen"] is None
    assert dashboard["holdings"] == dashboard["reservations"] == []
    assert "SYNTHETIC_" not in json.dumps(dashboard)
    assert "input_value" not in json.dumps(dashboard)
    assert app.read_bytes() == before
    assert not market.exists()


@pytest.mark.parametrize(
    "failed_source",
    [
        "calendar",
        "names",
        "names_empty",
        "names_missing",
        "names_stat",
        "valuation_schema",
        "valuation_sql",
        "valuation_resolve",
    ],
)
def test_optional_market_read_failure_does_not_block_independent_view_export(
    tmp_path, mocker, caplog, failed_source
):
    now = datetime.fromisoformat("2026-07-20T09:00:00+09:00")
    for module in (
        "baibai_web.sources.db_sources",
        "baibai_web.readmodel.builders.dashboard",
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
        elif failed_source.startswith("names"):
            connection.execute(
                "INSERT INTO jquants_market_calendar(day, holiday_division) VALUES (?, '1')",
                (now.date().isoformat(),),
            )
        elif failed_source == "valuation_schema":
            connection.execute("PRAGMA user_version=1")
    if failed_source == "valuation_sql":
        market.write_bytes(b"not a SQLite database")
    before = app.read_bytes(), market.read_bytes()
    if failed_source in ("names_stat", "valuation_resolve"):
        method = "stat" if failed_source == "names_stat" else "resolve"
        original = getattr(Path, method)

        def denied(path, *args, **kwargs):
            if path == market:
                raise PermissionError("SYNTHETIC_MARKET_PATH")
            return original(path, *args, **kwargs)

        mocker.patch.object(Path, method, denied)
    valuation_failed = failed_source.startswith("valuation_")
    if valuation_failed:
        error_type = {
            "valuation_schema": SQLiteSchemaError,
            "valuation_sql": sqlite3.Error,
            "valuation_resolve": PermissionError,
        }[failed_source]
        with pytest.raises(error_type):
            current_portfolio(service.load(), sqlite_path=market, now=now)

    listing = None
    if failed_source.startswith("names"):
        row = ListingMasterRow(
            ("XTKS", "23310"),
            {"names_empty": "", "names_missing": None}.get(failed_source, "保有企業"),
            "common_stock",
            "listed",
            japan=None
            if failed_source == "names"
            else JapanListingAttributes("011", "プライム", "サービス業"),
        )
        listing = CurrentListingMarket(
            ListingSlice("JP", now.date(), (row,)),
            source="jquants-equities-master",
            received_at=now - timedelta(minutes=1),
            received_count=1,
        )
    output = tmp_path / "export"
    paths = export_read_models(tmp_path, output, current_jp_listing=listing)

    assert {path.name for path in paths} == {
        "portfolio-dashboard.json",
        "outcomes.json",
        "latest-proposal.json",
        "proposal-history.json",
        "latest-us-proposal.json",
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
        assert dashboard["valuation_error"] == "市場評価を読み取れません。"
    else:
        assert dashboard["valuation_error"] is None
    if failed_source.startswith("names"):
        diagnostic = "PermissionError" if failed_source == "names_stat" else "JQuantsProviderError"
        assert f"tickerで表示します ({diagnostic})" in caplog.text
    assert "SYNTHETIC_MARKET_PATH" not in caplog.text
    assert all("SYNTHETIC_MARKET_PATH" not in path.read_text() for path in paths)
    outcomes = json.loads((output / "views/outcomes.json").read_text())
    assert outcomes["outcomes"][0]["portfolio_twr_pct"] == 10.2
    assert (app.read_bytes(), market.read_bytes()) == before


@pytest.mark.parametrize(
    "damage",
    [
        "invalid_payload",
        "missing_table",
        "wrong_id",
        "wrong_period",
        "wrong_metrics",
        "overflow_float",
        "overflow_integer",
        "duplicate_period",
        "overflow_legacy_float",
    ],
)
def test_outcome_read_failure_does_not_block_independent_views(tmp_path, damage):
    app = tmp_path / APPLICATION_DB_PATH
    ledger = load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    seed_ledger(app, ledger)
    PortfolioOutcomeStore(app).publish(_publication())
    with sqlite3.connect(app) as connection:
        if damage == "invalid_payload":
            connection.execute("UPDATE portfolio_outcome SET payload = '{}' ")
        elif damage == "missing_table":
            connection.execute("DROP TABLE portfolio_outcome")
        elif damage.startswith("overflow_"):
            # These values are legal JSON text but cannot describe a finite return.
            version = 1 if damage == "overflow_legacy_float" else 2
            number = "9" * 315 if damage == "overflow_integer" else "1e309"
            connection.execute(
                "UPDATE portfolio_outcome SET payload = json_set(payload, "
                "'$.schema_version', ?, '$.portfolio_twr_pct', json(?), "
                "'$.excess_percentage_points', json(?))",
                (version, number, number),
            )
        elif damage == "duplicate_period":
            connection.execute(
                "UPDATE portfolio_outcome SET payload = substr(payload, 1, length(payload) - 1) || ?",
                (',"period_end_date":"2099-01-01","period_end_date":"2025-12-31"}',),
            )
        else:
            field, value = {
                "wrong_id": ("outcome_id", "not-the-sqlite-row-id"),
                "wrong_period": ("period_end_date", "2099-01-01"),
                "wrong_metrics": ("portfolio_twr_pct", 91.0),
            }[damage]
            connection.execute(
                "UPDATE portfolio_outcome SET payload = json_set(payload, ?, ?)",
                (f"$.{field}", value),
            )
    before = app.read_bytes()

    output = tmp_path / "export"
    paths = export_read_models(tmp_path, output)

    assert len(paths) == 5
    dashboard = json.loads((output / "views/portfolio-dashboard.json").read_text())
    assert dashboard["ledger_error"] is None
    assert dashboard["holdings"][0]["quantity"] == 200
    outcomes = json.loads((output / "views/outcomes.json").read_text())
    assert outcomes == {"error": "保存済み運用成績を読み取れません。", "outcomes": []}
    assert app.read_bytes() == before


def test_outcomes_distinguish_absent_store_and_valid_empty_store(tmp_path):
    from baibai_web.readmodel.builders.outcomes import build_outcomes

    app = tmp_path / "app.sqlite"
    assert build_outcomes(app).error == "保存済み運用成績のstoreがありません。"
    assert not app.exists()
    app.touch()
    assert build_outcomes(app).error == "保存済み運用成績のstoreがありません。"
    assert app.stat().st_size == 0
    initialize_database(app)
    assert build_outcomes(app).model_dump() == {"error": None, "outcomes": []}


@pytest.mark.parametrize("problem", ["directory", "ancestor_file", "denied", "wrong_schema"])
def test_materialize_unreadable_app_is_not_an_unwritten_store(tmp_path, monkeypatch, problem):
    app = tmp_path / APPLICATION_DB_PATH
    app.parent.mkdir(parents=True)
    if problem == "directory":
        app.mkdir()
    elif problem == "ancestor_file":
        app.parent.rmdir()
        app.parent.write_bytes(b"not a store directory")
    elif problem == "wrong_schema":
        with closing(sqlite3.connect(app)) as connection:
            connection.execute("CREATE TABLE other_store (value INTEGER)")

    with monkeypatch.context() as patch:
        if problem == "denied":
            stat = Path.stat

            def denied(path, *args, **kwargs):
                if path == app:
                    raise PermissionError("SYNTHETIC_PRIVATE_PATH")
                return stat(path, *args, **kwargs)

            patch.setattr(Path, "stat", denied)
        paths = export_read_models(tmp_path, tmp_path / "export")

    exported = {path.name: json.loads(path.read_text()) for path in paths}
    assert len(exported) == 5
    ledger = exported["portfolio-dashboard.json"]
    assert ledger["ledger_exists"] is True
    assert ledger["ledger_error"] == "保存済み台帳を読み取れません。"
    assert ledger["available_cash_yen"] is ledger["total_capital_yen"] is None
    assert ledger["holdings"] == []
    assert exported["outcomes.json"] == {
        "error": "保存済み運用成績を読み取れません。",
        "outcomes": [],
    }
    for filename in ("latest-proposal.json", "proposal-history.json", "latest-us-proposal.json"):
        assert exported[filename]["status"] == "unavailable"
    assert "SYNTHETIC_PRIVATE_PATH" not in json.dumps(exported)
    assert not (tmp_path / MARKET_DB_PATH).exists()


@pytest.mark.parametrize(
    ("mutations", "expected"),
    [
        ({"portfolio_twr_pct": '"0"'}, (None, 8.2)),
        ({"benchmark_cumulative_return_pct": "false"}, (10.2, None)),
        ({"portfolio_twr_pct": "0", "benchmark_cumulative_return_pct": None}, (0.0, None)),
    ],
)
def test_retained_outcome_projects_only_numeric_returns_without_losing_a_sibling(
    tmp_path, mutations, expected
):
    app = tmp_path / APPLICATION_DB_PATH
    current = _publication()
    retained = replace(current, outcome_id="synthetic-retained")
    store = PortfolioOutcomeStore(app)
    store.publish(current)
    store.publish(retained)
    with closing(sqlite3.connect(app)) as connection, connection:
        connection.execute(
            "UPDATE portfolio_outcome SET payload = json_set(payload, '$.schema_version', 1)"
            " WHERE outcome_id=?",
            (retained.outcome_id,),
        )
        for field, encoded in mutations.items():
            if encoded is None:
                connection.execute(
                    "UPDATE portfolio_outcome SET payload = json_remove(payload, ?)"
                    " WHERE outcome_id=?",
                    (f"$.{field}", retained.outcome_id),
                )
            else:
                connection.execute(
                    "UPDATE portfolio_outcome SET payload = json_set(payload, ?, json(?))"
                    " WHERE outcome_id=?",
                    (f"$.{field}", encoded, retained.outcome_id),
                )
    before = app.read_bytes()

    output = tmp_path / "export"
    export_read_models(tmp_path, output)

    shown = json.loads((output / "views/outcomes.json").read_text())
    assert shown["error"] is None
    assert {
        row["outcome_id"]: (row["portfolio_twr_pct"], row["benchmark_cumulative_return_pct"])
        for row in shown["outcomes"]
    } == {current.outcome_id: (10.2, 8.2), retained.outcome_id: expected}
    assert app.read_bytes() == before
