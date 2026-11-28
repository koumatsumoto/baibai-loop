"""Reported quantities remain visible without ledger price transcriptions."""

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger
from tests.helpers.screening_sqlite import add_source_coverage, seed_daily_bars

from baibai_engine.machine_analysis.observations.stocks.listing_master import (
    JapanListingAttributes,
    ListingMasterRow,
    ListingSlice,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    CurrentListingMarket,
)
from baibai_engine.portfolio.ledger.drafts import (
    apply_draft,
    build_sell_execution_draft,
    build_share_conversion_draft,
)
from baibai_engine.portfolio.ledger.events import ContributionEvent, PortfolioLedgerDocument
from baibai_engine.portfolio.ledger.read import (
    LedgerConflictError,
    ledger_append_head,
    load_ledger_document,
    load_ledger_events,
)
from baibai_engine.portfolio.ledger.store import LedgerStoreService, _event_values
from baibai_engine.storage.paths import APPLICATION_DB_PATH, MARKET_DB_PATH
from baibai_web.materialize import export_read_models
from baibai_web.readmodel.builders import dashboard
from baibai_web.readmodel.builders.dashboard import build_dashboard
from baibai_web.readmodel.builders.presentation import percentage
from baibai_web.sources import db_sources
from baibai_web.sources.db_sources import (
    CurrentJpMarketSource,
    DbLedgerSource,
)

NOW = datetime.fromisoformat("2026-09-07T18:00:00+09:00")


@pytest.mark.parametrize(
    ("numerator", "denominator", "expected"),
    [(0, 0, None), (100, 0, None), (None, 100, None), (0, 100, 0.0), (10, 100, 10.0)],
)
def test_display_percentage_keeps_undefined_denominator_distinct_from_zero(
    numerator, denominator, expected
):
    assert percentage(numerator, denominator, digits=2) == expected


def test_display_percentage_can_preserve_the_ratio_until_the_formatter():
    assert percentage(Decimal("1049"), 100_000, digits=None) == 1.049
    assert percentage(Decimal("1049"), 100_000, digits=2) == 1.05


@pytest.fixture
def holding_case(tmp_path):
    """台帳/市場だけの合成保有fixture。旧評価publicationへ依存しない。"""
    db, market = tmp_path / "app.sqlite", tmp_path / "market.sqlite"
    ledger = load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    seed_ledger(db, ledger)
    seed_daily_bars(
        market,
        [
            ("2331", (date(2026, 6, 3) + timedelta(days=i)).isoformat(), 1000.0, 1.0)
            for i in range(94)
            if (date(2026, 6, 3) + timedelta(days=i)).weekday() < 5
        ],
    )
    start, end = date(2026, 6, 3), NOW.date()
    days = [start + timedelta(days=index) for index in range((end - start).days + 1)]
    with closing(sqlite3.connect(market)) as connection, connection:
        connection.executemany(
            "INSERT INTO jquants_market_calendar(day, holiday_division) VALUES (?, ?)",
            [(day.isoformat(), "1" if day.weekday() < 5 else "0") for day in days],
        )
        add_source_coverage(
            connection,
            source="jquants_market_calendar",
            coverage_key="synthetic-holding-calendar",
            min_date=start.isoformat(),
            max_date=end.isoformat(),
            record_count=len(days),
        )
    return db, market, ledger


@pytest.mark.parametrize("missing", [False, True])
def test_dashboard_reads_market_without_ledger_quotes_and_retains_unknowns(
    holding_case, tmp_path, monkeypatch, missing
):
    db, market, ledger = holding_case

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    monkeypatch.setattr(dashboard, "datetime", Clock)
    with closing(sqlite3.connect(market)) as connection, connection:
        connection.execute("UPDATE jquants_daily_bars SET close=NULL WHERE traded_at='2026-07-01'")
        if missing:
            connection.execute(
                "UPDATE jquants_daily_bars SET close=NULL WHERE traded_at='2026-09-04'"
            )
    listing = CurrentListingMarket(
        ListingSlice(
            "JP",
            NOW.date(),
            (
                ListingMasterRow(
                    ("XTKS", "23310"),
                    "保有企業",
                    "common_stock",
                    "listed",
                    japan=JapanListingAttributes("011", "プライム", "サービス業"),
                ),
                # An unrelated row whose missing metadata must not hide the held issue.
                ListingMasterRow(("XTKS", "99990"), None, "common_stock", "listed"),
            ),
        ),
        source="jquants-equities-master",
        received_at=NOW - timedelta(minutes=1),
        received_count=2,
    )
    before = db.read_bytes()
    view = build_dashboard(
        DbLedgerSource(db, market),
        CurrentJpMarketSource(market, listing=listing),
    )
    assert view.ledger_error is None
    assert view.valuation_error is None
    assert view.holding_quantity_unresolved is False
    assert view.ledger_as_of == ledger.as_of
    assert view.generated_at == NOW
    assert view.available_cash_yen == 10080500
    assert view.holdings[0].company_name == "保有企業"
    assert view.holdings[0].quantity == 200
    assert view.holdings[0].deployed_cost_yen == 203000
    assert view.holdings[0].market_value_yen == (None if missing else 200000)
    assert view.total_capital_yen == (None if missing else 10399500)
    assert view.holdings[0].market_price_as_of == (None if missing else date(2026, 9, 4))
    assert view.valuation_as_of == (None if missing else date(2026, 9, 4))
    assert view.model_dump(mode="json")["valuation_as_of"] == (None if missing else "2026-09-04")
    assert view.realized_gross_pnl_yen == 0
    if missing:
        assert view.holdings[0].unrealized_pnl_yen is None
        assert view.cash_pct is None
    assert db.read_bytes() == before


@pytest.mark.parametrize(
    ("price", "buy_price", "split_by", "deployed_cost", "displayed_value", "pnl_yen", "pnl_pct"),
    [
        (79.8, "4", 20, 400, 399, -1, -0.25),
        (79.9, "4", 20, 400, 400, 0, -0.125),
        (80.0, "4", 20, 400, 400, 0, 0.0),
        (80.1, "4", 20, 400, 401, 1, 0.125),
        (None, "4", 20, 400, None, None, None),
        # A sole position's 1.0479...% must not become 1.1% by rounding twice.
        pytest.param(
            202.5,
            "50.10",
            4,
            5010,
            5063,
            53,
            float(Decimal("52.5") / 5010 * 100),
            id="single-holding-near-display-rounding",
        ),
    ],
)
def test_dashboard_exports_holding_and_total_returns_before_rounding_the_value_to_yen(
    tmp_path, mocker, price, buy_price, split_by, deployed_cost, displayed_value, pnl_yen, pnl_pct
):
    app, market = tmp_path / APPLICATION_DB_PATH, tmp_path / MARKET_DB_PATH
    ledger = PortfolioLedgerDocument.model_validate(
        {
            "schema_version": 3,
            "portfolio_scope": "repository_only",
            "as_of": NOW,
            "events": [
                {
                    "type": "opening_balance",
                    "event_id": "cash",
                    "occurred_at": "2026-09-04T08:00:00+09:00",
                    "amount_yen": deployed_cost + 600,
                },
                {
                    "type": "execution",
                    "event_id": "buy",
                    "execution_id": "buy",
                    "occurred_at": "2026-09-04T09:15:00+09:00",
                    "ticker": "2331",
                    "side": "buy",
                    "quantity": 100,
                    "price_yen": buy_price,
                    "sector": "synthetic",
                },
            ],
        }
    )
    seed_ledger(app, ledger)
    service = LedgerStoreService(app)
    conversion = build_share_conversion_draft(
        service,
        ticker="2331",
        action_id="synthetic-consolidation",
        basis_date=NOW.date(),
        confirmed_at=NOW,
        now=NOW,
        source_ref="synthetic:confirmed",
        numerator=1,
        denominator=split_by,
        quantity_before=100,
        quantity_after=100 // split_by,
    )
    assert conversion is not None
    apply_draft(service, conversion, human_confirmed=True, now=NOW)
    seed_daily_bars(market, [("2331", NOW.date().isoformat(), price, float(split_by))])
    with closing(sqlite3.connect(market)) as connection, connection:
        connection.execute("UPDATE jquants_daily_bars SET ex_right_type='2'")
        connection.executemany(
            "INSERT INTO jquants_market_calendar VALUES (?, ?)",
            [
                ("2026-09-05", "0"),
                ("2026-09-06", "0"),
                ("2026-09-07", "1"),
            ],
        )
        add_source_coverage(
            connection,
            source="jquants_market_calendar",
            coverage_key="synthetic-odd-lot-calendar",
            min_date="2026-09-05",
            max_date="2026-09-07",
            record_count=3,
        )
    mocker.patch.object(dashboard, "datetime").now.return_value = NOW
    before = app.read_bytes(), market.read_bytes()

    output = tmp_path / "web-out"
    export_read_models(tmp_path, output)
    payload = json.loads((output / "views/portfolio-dashboard.json").read_text())

    assert payload["ledger_error"] is None
    assert payload["valuation_error"] is None
    assert payload["available_cash_yen"] == 600
    assert payload["deployed_cost_yen"] == deployed_cost
    assert payload["holdings_market_value_yen"] == displayed_value
    assert payload["holdings"][0]["unrealized_pnl_yen"] == pnl_yen
    assert payload["holdings"][0]["unrealized_pnl_pct"] == pnl_pct
    assert payload["unrealized_pnl_yen"] == pnl_yen
    assert payload["unrealized_pnl_pct"] == pnl_pct
    assert (app.read_bytes(), market.read_bytes()) == before


def test_dashboard_uses_one_clock_across_market_close(holding_case, monkeypatch):
    db, market, _ = holding_case
    before_close = datetime.fromisoformat("2026-09-07T15:29:59+09:00")
    close = datetime.fromisoformat("2026-09-07T15:30:00+09:00")
    instants = iter((before_close, close))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = next(instants)
            return instant if tz is None else instant.astimezone(tz)

    monkeypatch.setattr(dashboard, "datetime", Clock)
    # A second wall-clock read would admit the same-day bar across the close.
    monkeypatch.setattr(db_sources, "datetime", Clock)
    with closing(sqlite3.connect(market)) as connection, connection:
        connection.execute(
            "INSERT INTO jquants_daily_bars (ticker,traded_at,close,adjustment_factor) "
            "VALUES ('2331','2026-09-07',1400,1)"
        )
    before = db.read_bytes(), market.read_bytes()

    view = build_dashboard(DbLedgerSource(db, market), CurrentJpMarketSource(market))
    assert view.generated_at == before_close
    assert view.valuation_as_of == date(2026, 9, 4)
    assert view.holdings[0].market_value_yen == 200_000
    assert (db.read_bytes(), market.read_bytes()) == before


@pytest.mark.parametrize("missing", [False, True])
def test_dashboard_retains_realized_pnl_when_market_price_is_missing(holding_case, mocker, missing):
    db, market, _ = holding_case
    mocker.patch.object(dashboard, "datetime").now.return_value = NOW
    service = LedgerStoreService(db)
    draft = build_sell_execution_draft(
        service,
        occurred_at=datetime(2026, 7, 12, 10, tzinfo=ZoneInfo("Asia/Tokyo")),
        ticker="2331",
        quantity=100,
        price_yen=Decimal("1100"),
        decision_reference="synthetic-human-sale-note",
        now=NOW,
    )
    apply_draft(service, draft, human_confirmed=True, now=NOW)
    if missing:
        with closing(sqlite3.connect(market)) as connection, connection:
            connection.execute(
                "UPDATE jquants_daily_bars SET close=NULL WHERE traded_at='2026-09-04'"
            )
    view = build_dashboard(
        DbLedgerSource(db, market),
        CurrentJpMarketSource(market),
    )
    assert view.realized_gross_pnl_yen == 6_000
    assert view.holdings[0].market_value_yen == (None if missing else 100_000)
    assert view.valuation_as_of == (None if missing else date(2026, 9, 4))
    assert view.total_capital_yen == (None if missing else 10_409_500)


def test_cash_only_dashboard_keeps_ledger_facts_without_inventing_a_quote_date(tmp_path, mocker):
    app, market = tmp_path / "app.sqlite", tmp_path / "absent-market.sqlite"
    ledger = load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    ledger = ledger.model_copy(update={"events": ledger.events[:2]})
    seed_ledger(app, ledger)
    for module in (db_sources, dashboard):
        mocker.patch.object(module, "datetime").now.return_value = NOW
    before = app.read_bytes()

    view = build_dashboard(DbLedgerSource(app, market), CurrentJpMarketSource(market))

    assert view.ledger_exists is True
    assert view.ledger_error is None
    assert view.valuation_error is None
    assert view.generated_at == NOW
    assert view.ledger_as_of == ledger.as_of  # An old last report does not expire known cash.
    assert view.available_cash_yen == view.total_capital_yen == 10_400_000
    assert view.reserved_cash_yen == view.holdings_market_value_yen == view.unrealized_pnl_yen == 0
    assert view.unrealized_pnl_pct is None  # No deployed-cost denominator.
    assert view.holdings == []
    assert view.valuation_as_of is None
    assert app.read_bytes() == before
    assert not market.exists()


@pytest.mark.parametrize("action", [False, True])
def test_reported_full_sale_does_not_turn_unresolved_share_units_into_cash_only_nav(
    holding_case, mocker, action
):
    db, market, _ = holding_case
    service = LedgerStoreService(db)
    draft = build_sell_execution_draft(
        service,
        occurred_at=NOW,
        ticker="2331",
        quantity=200,
        price_yen=Decimal(550),
        execution_id="synthetic-full-sale",
    )
    apply_draft(service, draft, human_confirmed=True)
    if action:
        with closing(sqlite3.connect(market)) as connection, connection:
            connection.execute(
                "UPDATE jquants_daily_bars SET adjustment_factor=0.5, ex_right_type='1' "
                "WHERE traded_at='2026-09-04'"
            )
    for module in (db_sources, dashboard):
        mocker.patch.object(module, "datetime").now.return_value = NOW
    before = db.read_bytes(), market.read_bytes()
    view = build_dashboard(DbLedgerSource(db, market), CurrentJpMarketSource(market))
    assert view.ledger_exists
    assert view.ledger_error is None
    assert view.holdings == []  # Do not invent an unreported replacement quantity.
    assert view.available_cash_yen == 10190500
    assert view.reserved_cash_yen == 119000
    assert view.ledger_as_of == NOW
    assert json.loads(view.model_dump_json())["holding_quantity_unresolved"] is action
    if action:
        assert view.valuation_error is not None
        assert "2331" in view.valuation_error
        assert "株数変更" in view.valuation_error
        assert view.total_capital_yen is view.holdings_market_value_yen is None
        assert view.realized_gross_pnl_yen is None
        assert view.cash_pct is view.reserved_pct is view.deployed_pct is None
        assert view.warnings == []
    else:
        assert view.valuation_error is None
        assert view.total_capital_yen == 10309500
        assert view.holdings_market_value_yen == 0
        assert view.realized_gross_pnl_yen == -93000
    assert (db.read_bytes(), market.read_bytes()) == before


@pytest.mark.parametrize("consistent", [True, False], ids=["valid_archive", "mismatched_identity"])
def test_dashboard_and_exact_ledger_bind_the_same_event_identity(
    holding_case, monkeypatch, consistent
):
    db, market, ledger = holding_case

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    monkeypatch.setattr(dashboard, "datetime", Clock)
    baseline = build_dashboard(DbLedgerSource(db, market), CurrentJpMarketSource(market))
    event = ContributionEvent(
        type="contribution",
        event_id="fixture-contribution",
        occurred_at=ledger.as_of,
        amount_yen=57_123,
    )
    with closing(sqlite3.connect(db)) as connection, connection:
        archived_head = ledger_append_head(connection)
        row = _event_values(archived_head + 1, event, len(ledger.events))
        if not consistent:
            # A typed payload with a different index ID is not an admitted ledger event.
            row = (row[0], "fixture-other-index-id", *row[2:])
        connection.execute("INSERT INTO ledger_event VALUES (?,?,?,?,?,?,?,?)", row)
    before = db.read_bytes(), market.read_bytes()

    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("BEGIN")
        assert load_ledger_events(connection, append_head=archived_head) == ledger.events
        if consistent:
            document = load_ledger_document(connection)
            assert document is not None
            assert document.events == (*ledger.events, event)
            assert document.events == load_ledger_events(connection, append_head=archived_head + 1)
        else:
            with pytest.raises(LedgerConflictError, match="ledger event identity differs"):
                load_ledger_events(connection, append_head=archived_head + 1)
            with pytest.raises(LedgerConflictError, match="ledger event identity differs"):
                load_ledger_document(connection)

    view = build_dashboard(DbLedgerSource(db, market), CurrentJpMarketSource(market))
    assert view.ledger_exists
    if consistent:
        assert view.ledger_error is None
        assert baseline.available_cash_yen is not None
        assert baseline.total_capital_yen is not None
        assert view.available_cash_yen == baseline.available_cash_yen + event.amount_yen
        assert view.total_capital_yen == baseline.total_capital_yen + event.amount_yen
        assert view.holdings == baseline.holdings
    else:
        assert view.ledger_error == "保存済み台帳を読み取れません。"
        assert view.available_cash_yen is None
        assert view.total_capital_yen is None
        assert view.holdings == view.reservations == []
    assert (db.read_bytes(), market.read_bytes()) == before
