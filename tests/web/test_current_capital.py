"""Reported quantities remain visible without ledger price transcriptions."""

import sqlite3
from datetime import datetime

import pytest
from tests.engine.test_position_review_v3 import NOW
from tests.engine.test_position_review_v3 import holding_case as holding_case

from baibai_web.readmodel.builders import build_dashboard
from baibai_web.sources import db_sources
from baibai_web.sources.db_sources import (
    DbLedgerSource,
    DbMarketPriceSource,
    DbResearchSource,
    DbScreeningSource,
)


@pytest.mark.parametrize("missing", [False, True])
def test_dashboard_reads_market_without_ledger_quotes_and_retains_unknowns(
    holding_case, tmp_path, monkeypatch, missing
):
    db, market, _ = holding_case

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    monkeypatch.setattr(db_sources, "datetime", Clock)
    with sqlite3.connect(market) as connection:
        connection.execute("UPDATE jquants_daily_bars SET close=NULL WHERE traded_at='2026-07-01'")
        if missing:
            connection.execute(
                "UPDATE jquants_daily_bars SET close=NULL WHERE traded_at='2026-09-04'"
            )
    before = db.read_bytes()
    view = build_dashboard(
        DbLedgerSource(db, market),
        DbResearchSource(db),
        DbScreeningSource(tmp_path / "missing.sqlite"),
        DbMarketPriceSource(market),
    )
    assert view.ledger_error is None
    assert view.available_cash_yen == 10080500
    assert view.holdings[0].quantity == 200
    assert view.holdings[0].deployed_cost_yen == 203000
    assert view.holdings[0].market_value_yen == (None if missing else 200000)
    assert view.total_capital_yen == (None if missing else 10399500)
    if missing:
        assert view.holdings[0].unrealized_pnl_yen is None
        assert view.cash_pct is None
    assert db.read_bytes() == before
