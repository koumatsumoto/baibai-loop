"""A reported full sale must not resolve an observed, unrecorded share-unit change."""

import sqlite3
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal

import pytest
import yaml
from tests.engine.portfolio.holdings.test_current_quotes import _seed_calendar
from tests.helpers.db_seed import seed_ledger
from tests.helpers.screening_sqlite import seed_daily_bars

from baibai_engine.cli import portfolio as portfolio_cli
from baibai_engine.investment_judgment.trade_proposal.inputs import collect_proposal_inputs
from baibai_engine.portfolio.holdings.valuation import current_portfolio
from baibai_engine.portfolio.ledger.drafts import apply_draft, build_sell_execution_draft
from baibai_engine.portfolio.ledger.events import PortfolioLedgerDocument
from baibai_engine.portfolio.ledger.store import LedgerStoreService
from baibai_engine.storage.sqlite.application.read import connect_read_only

NOW = datetime.fromisoformat("2026-10-07T18:00:00+09:00")
SOLD = datetime.fromisoformat("2026-10-05T14:00:00+09:00")


@pytest.fixture
def closed_case(tmp_path):
    app, market = tmp_path / "application.sqlite", tmp_path / "market.sqlite"
    ledger = PortfolioLedgerDocument.model_validate(
        {
            "schema_version": 3,
            "portfolio_scope": "repository_only",
            "as_of": "2026-10-01T10:00:00+09:00",
            "events": [
                {
                    "type": "opening_balance",
                    "event_id": "cash",
                    "occurred_at": "2026-10-01T09:00:00+09:00",
                    "amount_yen": 1000000,
                },
                {
                    "type": "execution",
                    "event_id": "buy",
                    "execution_id": "buy",
                    "occurred_at": "2026-10-01T10:00:00+09:00",
                    "ticker": "1234",
                    "side": "buy",
                    "quantity": 100,
                    "price_yen": 1000,
                    "sector": "合成業種",
                },
            ],
        }
    )
    seed_ledger(app, ledger)
    service = LedgerStoreService(app)
    # Human-reported fills remain recordable without connecting to any market store.
    draft = build_sell_execution_draft(
        service,
        occurred_at=SOLD,
        ticker="1234",
        quantity=100,
        price_yen=Decimal(550),
        execution_id="reported-sale",
    )
    apply_draft(service, draft, human_confirmed=True)
    return app, market, service.load()


def _collect(app, market):
    with closing(connect_read_only(app)) as connection:
        connection.execute("BEGIN")
        return collect_proposal_inputs(
            connection,
            tickers=(),
            proposal_id="",
            now=NOW,
            current_listing=None,
            sqlite_path=market,
            budget_min_yen=200000,
            budget_max_yen=300000,
        )


@pytest.mark.parametrize(
    ("day", "factor", "kind", "unresolved"),
    [
        ("2026-10-05", 0.5, "1", True),
        ("2026-10-02", 2.0, "2", True),
        ("2026-10-02", 0.5, None, True),
        ("2026-10-02", None, "1", True),
        ("2026-10-02", 0.8, "3", False),
        ("2026-10-02", 1.0, None, False),
        ("2026-10-02", None, None, False),
        ("2026-10-01", 0.5, "1", False),
        ("2026-10-06", 0.5, "1", False),
    ],
)
def test_closed_position_checks_received_actions_not_missing_prices_or_calendar(
    closed_case, day, factor, kind, unresolved
):
    app, market, ledger = closed_case
    # No calendar or current close: neither is needed to contradict a closed quantity.
    seed_daily_bars(market, [("1234", day, None, factor)])
    with closing(sqlite3.connect(market)) as connection, connection:
        connection.execute("UPDATE jquants_daily_bars SET ex_right_type=?", (kind,))
    before = app.read_bytes(), market.read_bytes(), ledger.model_dump_json()
    if unresolved:
        for read in (
            lambda: current_portfolio(ledger, sqlite_path=market, now=NOW),
            lambda: _collect(app, market),
        ):
            with pytest.raises(ValueError, match=r"1234.*株数変更"):
                read()
    else:
        portfolio = current_portfolio(ledger, sqlite_path=market, now=NOW)
        inputs = _collect(app, market)
        assert portfolio.total_capital_yen == inputs.total_capital_yen == 955000
        assert portfolio.holdings == inputs.positions == ()
        assert portfolio.realized_gross_pnl_yen == -45000
    assert (app.read_bytes(), market.read_bytes(), ledger.model_dump_json()) == before


def test_normal_full_sale_needs_no_market_store(closed_case):
    app, market, ledger = closed_case
    before = app.read_bytes()
    assert current_portfolio(ledger, sqlite_path=market, now=NOW).total_capital_yen == 955000
    assert _collect(app, market).total_capital_yen == 955000
    assert app.read_bytes() == before
    assert not market.exists()


@pytest.mark.parametrize(("factor", "exit_code"), [(1.0, 0), (0.5, 2)])
def test_ledger_cli_distinguishes_unresolved_full_sale(
    closed_case, monkeypatch, capsys, factor, exit_code
):
    app, market, _ = closed_case
    seed_daily_bars(market, [("1234", "2026-10-02", None, factor)])
    monkeypatch.setattr(portfolio_cli, "MARKET_DB_PATH", market)
    before = app.read_bytes(), market.read_bytes()

    assert portfolio_cli.main(["ledger", "--db", str(app)], now=NOW) == exit_code
    captured = capsys.readouterr()
    if exit_code == 2:
        assert captured.out == ""
        assert captured.err.startswith("error: 1234:")
        assert "株数変更" in captured.err
    else:
        assert captured.err == ""
        assert yaml.safe_load(captured.out)["holdings"] == []
    assert (app.read_bytes(), market.read_bytes()) == before


@pytest.mark.parametrize("action_day", ["2026-10-02", "2026-10-06"])
def test_reentry_only_preserves_actions_received_during_a_holding(closed_case, action_day):
    _, market, ledger = closed_case
    raw = ledger.model_dump(mode="json")
    raw["events"].append(
        dict(
            raw["events"][1],
            event_id="reentry",
            execution_id="reentry",
            occurred_at=NOW.isoformat(),
        )
    )
    raw["as_of"] = NOW.isoformat()
    ledger = PortfolioLedgerDocument.model_validate(raw)
    seed_daily_bars(market, [("1234", action_day, None, 0.5)])
    if action_day == "2026-10-02":
        with pytest.raises(ValueError, match=r"1234.*株数変更"):
            current_portfolio(ledger, sqlite_path=market, now=NOW)
    else:
        snapshot = current_portfolio(ledger, sqlite_path=market, now=NOW)
        assert snapshot.holdings[0].quantity == 100


def test_read_cutoff_ignores_a_future_sale(closed_case):
    _, market, ledger = closed_case
    seed_daily_bars(market, [("1234", "2026-10-02", None, 0.5)])
    snapshot = current_portfolio(
        ledger, sqlite_path=market, now=datetime.fromisoformat("2026-10-02T18:00:00+09:00")
    )
    assert snapshot.holdings[0].quantity == 100
    assert snapshot.total_capital_yen is None


def _rebought_split_holdings_case(tmp_path, count, missing):
    app, market = tmp_path / "application.sqlite", tmp_path / "market.sqlite"
    other_tickers = tuple(str(2000 + index) for index in range(count - 1))
    tickers = ("1234", *other_tickers)
    missing_ticker = tickers[-1] if missing else None

    def trade(identity, day, ticker, side, quantity, price, **extra):
        return dict(
            type="execution",
            event_id=identity,
            execution_id=identity,
            occurred_at=f"2026-10-{day}T10:00:00+09:00",
            ticker=ticker,
            side=side,
            quantity=quantity,
            price_yen=price,
            sector="合成業種",
            **extra,
        )

    def split(day):
        return dict(
            type="share_conversion",
            event_id=f"split-{day}",
            action_id=f"split-{day}",
            occurred_at=f"2026-10-{day}T00:00:00+09:00",
            occurred_on=f"2026-10-{day}",
            confirmed_at=f"2026-10-{day}T18:00:00+09:00",
            ticker="1234",
            numerator=2,
            denominator=1,
            quantity_before=100,
            quantity_after=200,
            source_ref="synthetic-confirmation",
        )

    ledger = PortfolioLedgerDocument.model_validate(
        dict(
            schema_version=4,
            portfolio_scope="repository_only",
            as_of=NOW,
            events=(
                dict(
                    type="opening_balance",
                    event_id="cash",
                    occurred_at="2026-10-01T09:00:00+09:00",
                    amount_yen=3000000,
                ),
                trade("old-buy", "01", "1234", "buy", 100, 1000, decision_reference="old-origin"),
                split("02"),
                trade("full-exit", "05", "1234", "sell", 200, 530),
                trade(
                    "reentry", "06", "1234", "buy", 100, 550, decision_reference="reentry-origin"
                ),
                *(
                    trade(f"buy-{ticker}", "06", ticker, "buy", 100, 100)
                    for ticker in other_tickers
                ),
                split("07"),
            ),
        )
    )
    seed_ledger(app, ledger)
    seed_daily_bars(
        market,
        [
            ("1234", "2026-10-02", 530, 0.5),
            ("1234", "2026-10-06", 550, 1.0),
            *(
                (ticker, "2026-10-07", None if ticker == missing_ticker else close, factor)
                for ticker, close, factor in (
                    ("1234", 280, 0.5),
                    *((ticker, 120, 1.0) for ticker in other_tickers),
                )
            ),
        ],
    )
    _seed_calendar(market, date(2026, 10, 1), NOW.date())
    with closing(sqlite3.connect(market)) as connection, connection:
        connection.execute(
            "UPDATE jquants_daily_bars SET ex_right_type='1' WHERE ticker='1234' AND adjustment_factor=.5"
        )
    return app, market, ledger, missing_ticker


@pytest.mark.parametrize("count", [1, 8])
@pytest.mark.parametrize("missing", [False, True])
def test_proposal_and_valuation_prepare_holding_episodes_once(tmp_path, mocker, count, missing):
    from baibai_engine.portfolio.holdings import valuation
    from baibai_engine.portfolio.ledger import events

    app, market, ledger, missing_ticker = _rebought_split_holdings_case(tmp_path, count, missing)
    before = app.read_bytes(), market.read_bytes()
    episodes = mocker.spy(valuation, "holding_episodes")
    conversions = mocker.spy(valuation, "share_conversions")
    ordered_replay = mocker.spy(events, "ordered_replay_events")
    snapshot = current_portfolio(ledger, sqlite_path=market, now=NOW)
    assert (episodes.call_count, conversions.call_count, ordered_replay.call_count) == (1, 1, 2)
    episodes.reset_mock()
    conversions.reset_mock()
    ordered_replay.reset_mock()

    inputs = _collect(app, market)
    cash = 2951000 - (count - 1) * 10000
    total = None if missing else cash + 56000 + (count - 1) * 12000
    assert snapshot.available_cash_yen == inputs.available_cash_yen == cash
    assert snapshot.total_capital_yen == inputs.total_capital_yen == total
    assert snapshot.realized_gross_pnl_yen == 6000
    assert snapshot.holdings[0].quantity == inputs.positions[0].quantity == 200
    assert snapshot.holdings[0].deployed_cost_yen == inputs.positions[0].cost_yen == 55000
    assert "reentry-origin" in inputs.positions[0].original_reason_unavailable_reason
    assert [row.ticker for row in inputs.positions if not row.quantity_basis_confirmed] == (
        [missing_ticker] if missing else []
    )
    assert [row.ticker for row in snapshot.holdings if row.market_value_yen is None] == (
        [missing_ticker] if missing else []
    )
    assert (app.read_bytes(), market.read_bytes()) == before
    assert (episodes.call_count, conversions.call_count, ordered_replay.call_count) == (1, 1, 2)
