"""Reported quantities remain visible without ledger price transcriptions."""

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

from baibai_engine.portfolio.ledger.drafts import apply_draft, build_sell_execution_draft
from baibai_engine.portfolio.ledger.store import LedgerStoreService
from baibai_web.readmodel.builders import dashboard
from baibai_web.readmodel.builders.dashboard import build_dashboard
from baibai_web.readmodel.builders.presentation import percentage
from baibai_web.sources import db_sources
from baibai_web.sources.db_sources import (
    DbLedgerSource,
    DbMarketSource,
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

    monkeypatch.setattr(db_sources, "datetime", Clock)
    monkeypatch.setattr(dashboard, "datetime", Clock)
    with closing(sqlite3.connect(market)) as connection, connection:
        connection.execute("UPDATE jquants_daily_bars SET close=NULL WHERE traded_at='2026-07-01'")
        if missing:
            connection.execute(
                "UPDATE jquants_daily_bars SET close=NULL WHERE traded_at='2026-09-04'"
            )
    before = db.read_bytes()
    view = build_dashboard(
        DbLedgerSource(db, market),
        DbMarketSource(market),
    )
    assert view.ledger_error is None
    assert view.ledger_as_of == ledger.as_of
    assert view.generated_at == NOW
    assert view.available_cash_yen == 10080500
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


@pytest.mark.parametrize("missing", [False, True])
def test_dashboard_retains_realized_pnl_when_market_price_is_missing(
    holding_case, tmp_path, missing
):
    db, market, _ = holding_case
    service = LedgerStoreService(db)
    draft = build_sell_execution_draft(
        service,
        occurred_at=datetime(2026, 7, 12, 10, tzinfo=ZoneInfo("Asia/Tokyo")),
        ticker="2331",
        quantity=100,
        price_yen=Decimal("1100"),
        decision_reference="synthetic-human-sale-note",
    )
    apply_draft(service, draft, human_confirmed=True)
    if missing:
        with closing(sqlite3.connect(market)) as connection, connection:
            connection.execute(
                "UPDATE jquants_daily_bars SET close=NULL WHERE traded_at='2026-09-04'"
            )
    view = build_dashboard(
        DbLedgerSource(db, market),
        DbMarketSource(market),
    )
    assert view.realized_gross_pnl_yen == 6_000
    if missing:
        assert view.total_capital_yen is None
