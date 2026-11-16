"""A missing calendar row cannot move cashflows and publish a different TWR."""

import sqlite3
from contextlib import closing

import pytest
import yaml
from tests.engine.portfolio.performance.test_calculate import _ledger, _prepare_outcome_cli

from baibai_engine.cli.portfolio import main
from baibai_engine.portfolio.ledger.events import PortfolioLedgerDocument
from baibai_engine.portfolio.performance.store import PortfolioOutcomeStore


@pytest.mark.parametrize("missing_day", [None, "2026-01-05", "2026-01-04"])
def test_cash_outcome_requires_the_calendar_rows_not_just_completed_coverage(
    tmp_path, capsys, missing_day
):
    raw = _ledger().model_dump(mode="json")
    raw["events"] = [
        raw["events"][0],
        {
            "event_id": "cost-before-funding",
            "type": "cost",
            "occurred_at": "2026-01-05T09:00:00+09:00",
            "cost_kind": "commission",
            "amount_yen": 100,
        },
        {
            "event_id": "new-funding",
            "type": "contribution",
            "occurred_at": "2026-01-06T09:00:00+09:00",
            "amount_yen": 1000,
        },
    ]
    args, application = _prepare_outcome_cli(tmp_path, PortfolioLedgerDocument.model_validate(raw))
    market = tmp_path / "market.sqlite"
    if missing_day is not None:
        with closing(sqlite3.connect(market)) as connection, connection:
            connection.execute("DELETE FROM jquants_market_calendar WHERE day = ?", (missing_day,))
    before_app, before_market = application.read_bytes(), market.read_bytes()

    assert main(args) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = yaml.safe_load(captured.out)
    if missing_day is None:
        assert payload["status"] == "resolved"
        # The 100-yen cost loses 10% before the next day's new capital arrives.
        assert payload["portfolio_twr_pct"] == pytest.approx(-10)
        assert payload["ending_cash_yen"] == 1900
        assert PortfolioOutcomeStore(application).list() == (payload,)
    else:
        assert payload["status"] == "unresolved"
        assert payload["reason"] == "benchmark_unavailable"
        assert payload.get("portfolio_twr_pct") is None
        assert "outcome_id" not in payload
        assert PortfolioOutcomeStore(application).list() == ()
        assert application.read_bytes() == before_app
    assert market.read_bytes() == before_market
