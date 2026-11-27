"""Cash NAV and cashflows need the exchange calendar, not a price acquisition receipt."""

import sqlite3
from contextlib import closing

import pytest
import yaml
from tests.engine.portfolio.performance.test_calculate import _ledger, _prepare_outcome_cli
from tests.helpers.screening_sqlite import add_source_coverage

from baibai_engine.cli.portfolio import main
from baibai_engine.portfolio.ledger.events import PortfolioLedgerDocument
from baibai_engine.portfolio.performance.store import PortfolioOutcomeStore


def _observed_outcome(tmp_path, capsys, trades, daily_inputs):
    raw = _ledger().model_dump(mode="json")
    raw["events"] = [raw["events"][0]]
    for i, (ticker, at, side, price) in enumerate(trades):
        raw["events"].append(
            dict(
                type="execution",
                event_id=f"fill-{i}",
                execution_id=f"fill-{i}",
                occurred_at=f"2026-01-{at}:00+09:00",
                ticker=ticker,
                side=side,
                quantity=100,
                price_yen=price,
                sector="synthetic" if side == "buy" else None,
            )
        )
    args, application = _prepare_outcome_cli(tmp_path, PortfolioLedgerDocument.model_validate(raw))
    market = tmp_path / "market.sqlite"
    with closing(sqlite3.connect(market)) as connection, connection:
        for ticker, day, close, factor, receipt in daily_inputs:
            day = f"2026-01-{day:02}"
            connection.execute(
                "INSERT INTO jquants_daily_bars(ticker,traded_at,close,adjustment_factor) "
                "VALUES (?,?,?,?)",
                (ticker, day, close, factor),
            )
            if receipt is not None:
                add_source_coverage(
                    connection,
                    source="jquants_daily_bars",
                    coverage_key=day,
                    min_date=day,
                    max_date=day,
                    status=receipt,
                )
    before = market.read_bytes()
    assert main(args) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = yaml.safe_load(captured.out)
    assert market.read_bytes() == before
    assert PortfolioOutcomeStore(application).list() == (
        (payload,) if payload["status"] == "resolved" else ()
    )
    return payload


@pytest.mark.parametrize("buy_at", ["06T09:00", "05T16:00"])
def test_only_final_close_is_needed_after_cash_only_sessions(tmp_path, capsys, buy_at):
    payload = _observed_outcome(
        tmp_path, capsys, [("1234", buy_at, "buy", 5)], [("1234", 6, 7, 1, "ok")]
    )
    assert payload["status"] == "resolved"
    assert payload["ending_holdings_market_value_yen"] == 700
    assert payload["portfolio_twr_pct"] == pytest.approx(20)


@pytest.mark.parametrize(
    ("sale_receipt", "sale_factor", "reason"),
    [
        ("ok", 1, None),
        ("partial", 1, "missing_market_price"),
        (None, 1, "missing_market_price"),
        ("ok", 0.5, "corporate_action_unresolved"),
    ],
)
def test_cash_gap_skips_price_but_earlier_sale_still_checks_basis(
    tmp_path, capsys, sale_receipt, sale_factor, reason
):
    # No Jan 5 input: all cash between the two episodes. Jan 2 needs factor
    # evidence through the sale but not a close once everything was sold.
    payload = _observed_outcome(
        tmp_path,
        capsys,
        [
            ("1234", "01T09:00", "buy", 5),
            ("1234", "02T09:00", "sell", 6),
            ("1234", "06T09:00", "buy", 5),
        ],
        [
            ("1234", 1, 5, 1, "ok"),
            ("1234", 2, None, sale_factor, sale_receipt),
            ("1234", 6, 7, 1, "ok"),
        ],
    )
    if reason is None:
        assert payload["status"] == "resolved"
        assert payload["portfolio_twr_pct"] == pytest.approx(30)
    else:
        assert (payload["status"], payload["reason"]) == ("unresolved", reason)


def test_close_is_required_on_an_after_close_sale_but_not_on_an_intraday_roundtrip(
    tmp_path, capsys
):
    intraday, after_close = tmp_path / "intraday", tmp_path / "after-close"
    intraday.mkdir()
    after_close.mkdir()
    buy = ("1234", "02T09:00", "buy", 5)
    settled = _observed_outcome(intraday, capsys, [buy, ("1234", "02T15:00", "sell", 6)], [])
    assert settled["status"] == "resolved"
    assert settled["portfolio_twr_pct"] == pytest.approx(10)
    unresolved = _observed_outcome(
        after_close, capsys, [buy, ("1234", "02T16:00", "sell", 6)], [("1234", 2, None, 1, "ok")]
    )
    assert (unresolved["status"], unresolved["reason"]) == ("unresolved", "missing_market_price")


@pytest.mark.parametrize(
    ("sale_factor", "reason"), [(1, None), (0.5, "corporate_action_unresolved")]
)
def test_only_held_ticker_closes_are_decoded_and_sale_basis_is_still_required(
    tmp_path, capsys, sale_factor, reason
):
    payload = _observed_outcome(
        tmp_path,
        capsys,
        [
            ("1234", "01T09:00", "buy", 5),
            ("1234", "02T09:00", "sell", 6),
            ("5678", "06T09:00", "buy", 6),
        ],
        [
            ("1234", 1, 5, 1, "ok"),
            # Before purchase, after a sale and on a sale-only session, no close is used.
            ("5678", 1, "unused", 0.5, None),
            ("1234", 2, "unused", sale_factor, "ok"),
            ("1234", 6, "unused", 0.5, None),
            ("5678", 6, 7, 1, "ok"),
        ],
    )
    if reason is None:
        assert payload["status"] == "resolved"
        assert payload["open_tickers"] == ["5678"]
        assert payload["ending_holdings_market_value_yen"] == 700
        assert payload["portfolio_twr_pct"] == pytest.approx(20)
    else:
        assert (payload["status"], payload["reason"]) == ("unresolved", reason)
