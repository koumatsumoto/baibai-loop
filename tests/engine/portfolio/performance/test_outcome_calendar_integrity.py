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


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("complete-sessions", None),
        ("partial-open", "missing_market_price"),
        ("unmaterialized-open", "missing_market_price"),
        ("missing-end-close", "missing_market_price"),
        ("closed-end", "basis_mismatch"),
        ("unknown-holiday", "benchmark_unavailable"),
        ("unexpected-holiday-action", "corporate_action_unresolved"),
    ],
)
def test_held_outcome_uses_confirmed_cash_sessions_for_daily_bar_coverage(
    tmp_path, capsys, case, reason
):
    args, application = _prepare_outcome_cli(tmp_path, _ledger())
    market = tmp_path / "market.sqlite"
    with closing(sqlite3.connect(market)) as connection, connection:
        # Synthetic native layout: only cash sessions receive bars. A futures-only
        # holiday has no price receipt; a half session still requires one.
        connection.execute(
            "UPDATE jquants_market_calendar SET holiday_division='3' WHERE day='2026-01-04'"
        )
        connection.execute(
            "UPDATE jquants_market_calendar SET holiday_division='2' WHERE day='2026-01-05'"
        )
        if case == "closed-end":
            connection.execute(
                "UPDATE jquants_market_calendar SET holiday_division='0' WHERE day='2026-01-06'"
            )
        connection.execute(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, close, adjustment_factor) "
            "SELECT '1234', day, CASE day WHEN '2026-01-06' THEN 7.0 ELSE 5.0 END, 1.0 "
            "FROM jquants_market_calendar WHERE holiday_division IN ('1','2')"
        )
        connection.execute(
            "INSERT INTO source_coverage(source,coverage_key,coverage_start,coverage_end,"
            "fetched_at_utc,record_count,status) "
            "SELECT 'jquants_daily_bars','native:'||day,day,day,'2026-01-08T00:00:00Z', "
            "holiday_division IN ('1','2'), CASE WHEN holiday_division IN ('1','2') "
            "THEN 'ok' ELSE 'unavailable' END FROM jquants_market_calendar"
        )
        if case == "partial-open":
            connection.execute(
                "UPDATE source_coverage SET status='partial' WHERE coverage_key='native:2026-01-05'"
            )
        elif case == "unmaterialized-open":
            connection.execute("DELETE FROM source_coverage WHERE coverage_key='native:2026-01-05'")
        elif case == "missing-end-close":
            connection.execute("DELETE FROM jquants_daily_bars WHERE traded_at='2026-01-06'")
        elif case == "unexpected-holiday-action":
            connection.execute(
                "INSERT INTO jquants_daily_bars(ticker, traded_at, adjustment_factor) "
                "VALUES ('1234', '2026-01-04', 0.5)"
            )
        elif case == "unknown-holiday":
            connection.execute(
                "UPDATE jquants_market_calendar SET holiday_division='9' WHERE day='2026-01-04'"
            )
    before_app, before_market = application.read_bytes(), market.read_bytes()

    assert main(args) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = yaml.safe_load(captured.out)
    if reason is None:
        assert payload["status"] == "resolved"
        assert payload["period_end_date"] == "2026-01-06"
        assert payload["ending_holdings_market_value_yen"] == 700
        assert payload["ending_cash_yen"] == 608
        assert payload["portfolio_twr_pct"] == pytest.approx(100 * 208 / 1100, abs=1e-6)
        assert PortfolioOutcomeStore(application).list() == (payload,)
    else:
        assert payload["status"] == "unresolved"
        assert payload["reason"] == reason
        assert payload.get("portfolio_twr_pct") is None
        assert PortfolioOutcomeStore(application).list() == ()
        assert application.read_bytes() == before_app
    assert market.read_bytes() == before_market
