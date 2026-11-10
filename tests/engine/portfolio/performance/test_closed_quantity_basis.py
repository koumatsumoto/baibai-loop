import sqlite3
from datetime import date, datetime

import pytest
import yaml
from tests.engine.portfolio.performance.test_calculate import (
    _cash_only_period_ledger,
    _prepare_outcome_cli,
)

from baibai_engine.cli.portfolio import main
from baibai_engine.portfolio.holdings.valuation import (
    UnresolvedHoldingQuantityError,
    current_portfolio,
)
from baibai_engine.portfolio.ledger.events import PortfolioLedgerDocument
from baibai_engine.portfolio.ledger.store import LedgerStoreService
from baibai_engine.portfolio.performance.calculate import outcome_ledger_at_close
from baibai_engine.portfolio.performance.store import PortfolioOutcomeStore


def _closed_before_period():
    raw = _cash_only_period_ledger("closed_before_start").model_dump(mode="json")
    for event in raw["events"][:3]:
        event["occurred_at"] = event["occurred_at"].replace("2024-12-27", "2024-12-20")
    raw["events"][3].update(occurred_at="2024-12-24T09:00:00+09:00", price_yen="2.5")
    return PortfolioLedgerDocument.model_validate(raw)


@pytest.mark.parametrize(
    ("factor", "ex_right_type", "unresolved"),
    [(0.5, "1", True), (2.0, "2", True), (None, "1", True), (1.0, None, False), (0.9, "3", False)],
)
def test_outcome_checks_received_preperiod_share_actions_without_requiring_old_prices(
    tmp_path, capsys, factor, ex_right_type, unresolved
):
    ledger = _closed_before_period()
    args, application = _prepare_outcome_cli(tmp_path, ledger)
    market = tmp_path / "market.sqlite"
    # No pre-period calendar, price coverage or closing price is supplied.
    with sqlite3.connect(market) as connection:
        connection.execute(
            "INSERT INTO jquants_daily_bars "
            "(ticker,traded_at,close,adjustment_factor,ex_right_type) VALUES (?,?,?,?,?)",
            ("1234", "2024-12-23", None, factor, ex_right_type),
        )
    if unresolved:
        with pytest.raises(UnresolvedHoldingQuantityError):
            current_portfolio(
                ledger, sqlite_path=market, now=datetime.fromisoformat("2026-01-06T16:00:00+09:00")
            )
    before = application.read_bytes()
    assert main(args) == 0
    payload = yaml.safe_load(capsys.readouterr().out)
    if unresolved:
        assert payload["status"] == "unresolved"
        assert payload["reason"] == "corporate_action_unresolved"
        assert payload.get("portfolio_twr_pct") is None
        assert "outcome_id" not in payload
        assert PortfolioOutcomeStore(application).list() == ()
        assert application.read_bytes() == before
    else:
        assert payload["status"] == "resolved"
        assert payload["portfolio_twr_pct"] == 0.0
        assert payload["ending_cash_yen"] == 750
        assert payload["ending_holdings_market_value_yen"] == 0
        assert PortfolioOutcomeStore(application).list() == (payload,)
        assert main(args) == 0
        assert yaml.safe_load(capsys.readouterr().out) == payload
    assert LedgerStoreService(application).load() == ledger


def test_unresolved_episode_after_period_end_does_not_change_prior_outcome(tmp_path, capsys):
    raw = _closed_before_period().model_dump(mode="json")
    for event in raw["events"][1:3]:
        event["occurred_at"] = event["occurred_at"].replace("2024-12-20", "2026-01-07")
    raw["events"][1]["expires_at"] = "2026-01-10T15:30:00+09:00"
    raw["events"][3]["occurred_at"] = "2026-01-09T09:00:00+09:00"
    raw["as_of"] = "2026-01-09T16:00:00+09:00"
    ledger = PortfolioLedgerDocument.model_validate(raw)
    args, application = _prepare_outcome_cli(tmp_path, ledger)
    with sqlite3.connect(tmp_path / "market.sqlite") as connection:
        connection.execute(
            "INSERT INTO jquants_daily_bars "
            "(ticker,traded_at,adjustment_factor,ex_right_type) VALUES ('1234','2026-01-08',0.5,'1')"
        )
    assert main(args) == 0
    payload = yaml.safe_load(capsys.readouterr().out)
    assert payload["status"] == "resolved"
    assert payload["portfolio_twr_pct"] == 0.0
    assert payload["ending_cash_yen"] == 1000
    assert LedgerStoreService(application).load() == ledger


@pytest.mark.parametrize("date_only", [False, True])
def test_quantity_check_uses_outcome_close_scope_not_internal_date_only_clock(date_only):
    raw = _cash_only_period_ledger("after_end_close_purchase").model_dump(mode="json")
    if date_only:
        for event in raw["events"][1:]:
            event["occurred_on"] = "2026-01-06"
    ledger = PortfolioLedgerDocument.model_validate(raw)
    scoped = outcome_ledger_at_close(
        ledger,
        business_days=(date(2025, 1, 6), date(2026, 1, 6)),
        start=date(2025, 1, 6),
        end=date(2026, 1, 6),
    )
    assert scoped.events == (ledger.events if date_only else ledger.events[:1])
    assert scoped.as_of == ledger.as_of
