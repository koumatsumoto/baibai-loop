"""Confirmed whole-share conversions preserve capital, order facts and frozen history."""

import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction

import pytest
from tests.engine.portfolio.holdings.test_current_quotes import _seed_calendar
from tests.engine.portfolio.performance.test_calculate import _benchmark
from tests.helpers.db_seed import seed_ledger
from tests.helpers.screening_sqlite import seed_daily_bars

from baibai_engine.cli.portfolio import main
from baibai_engine.investment_judgment.trade_proposal.inputs import collect_proposal_inputs
from baibai_engine.machine_analysis.observations.prices.price_models import JQuantsDailyBar
from baibai_engine.portfolio.holdings.valuation import current_portfolio, holding_episode
from baibai_engine.portfolio.ledger.drafts import (
    apply_draft,
    build_sell_execution_draft,
    build_share_conversion_draft,
    load_draft,
)
from baibai_engine.portfolio.ledger.events import (
    PortfolioLedgerDocument,
    PortfolioLedgerError,
    replay_events_through,
)
from baibai_engine.portfolio.ledger.read import load_ledger_events
from baibai_engine.portfolio.ledger.store import LedgerStoreService
from baibai_engine.portfolio.performance.calculate import compute_portfolio_outcome
from baibai_engine.storage.sqlite.application.read import connect_read_only

NOW = datetime.fromisoformat("2026-10-05T18:00:00+09:00")
BASIS = date(2026, 10, 2)


def _ledger(*, lots=1):
    events = [
        dict(
            type="opening_balance",
            event_id="cash",
            occurred_at="2026-10-01T08:00:00+09:00",
            amount_yen=1000000,
        )
    ]
    events += [
        dict(
            type="execution",
            event_id=f"buy-{i}",
            execution_id=f"buy-{i}",
            occurred_at=f"2026-10-01T10:0{i}:00+09:00",
            ticker="1234",
            side="buy",
            quantity=100,
            price_yen=1000 + i,
            sector="synthetic",
        )
        for i in range(lots)
    ]
    return PortfolioLedgerDocument.model_validate(
        dict(schema_version=3, portfolio_scope="repository_only", as_of=NOW, events=events)
    )


def _service(tmp_path, *, lots=1, document=None):
    path = tmp_path / "app.sqlite"
    seed_ledger(path, _ledger(lots=lots) if document is None else document)
    return LedgerStoreService(path)


def _draft(service, **overrides):
    args = dict(
        action_id="split-1234-20261002",
        ticker="1234",
        basis_date=BASIS,
        numerator=2,
        denominator=1,
        quantity_before=100,
        quantity_after=200,
        confirmed_at=NOW,
        source_ref="synthetic:human-confirmed",
    )
    args.update(overrides)
    return build_share_conversion_draft(service, **args)


def _apply(service, **overrides):
    draft = _draft(service, **overrides)
    assert draft is not None
    apply_draft(service, draft, human_confirmed=True)
    return service.load()


def _state(service):
    document = service.load()
    return replay_events_through(document.events, document.as_of)


def _sell(service, quantity, identity, *, when=NOW):
    draft = build_sell_execution_draft(
        service,
        occurred_at=when,
        ticker="1234",
        quantity=quantity,
        price_yen=Decimal(550),
        execution_id=identity,
    )
    assert draft is not None
    apply_draft(service, draft, human_confirmed=True)


@pytest.mark.parametrize(("n", "d", "after"), [(2, 1, 200), (3, 1, 300), (1, 2, 50)])
def test_conversion_keeps_cash_total_cost_and_old_prefix(tmp_path, n, d, after):
    service = _service(tmp_path)
    source, head = service.load_with_head()
    with sqlite3.connect(tmp_path / "app.sqlite") as connection:
        before = connection.execute(
            "SELECT append_seq,event_id,payload FROM ledger_event ORDER BY append_seq"
        ).fetchall()
    document = _apply(service, numerator=n, denominator=d, quantity_after=after)
    state = _state(service)
    assert sum(lot.quantity for lot in state.lots["1234"]) == after
    assert sum(lot.cost_yen for lot in state.lots["1234"]) == 100000
    assert (
        state.available_cash_yen,
        state.realized_gross_pnl_yen,
        state.confirmed_tax_yen,
        state.confirmed_income_yen,
    ) == (900000, 0, 0, 0)
    assert document.schema_version == 4
    with sqlite3.connect(tmp_path / "app.sqlite") as connection:
        assert (
            connection.execute(
                "SELECT append_seq,event_id,payload FROM ledger_event WHERE append_seq<=? ORDER BY append_seq",
                (head,),
            ).fetchall()
            == before
        )
        assert load_ledger_events(connection, append_head=head) == source.events
    assert source.schema_version == 3
    assert holding_episode(document, "1234", NOW)[0] == source.events[1]


@pytest.mark.parametrize("chunks", [(100, 100, 100), (200, 100), (300,)])
def test_three_split_partial_sales_allocate_original_fifo_cost_once(tmp_path, chunks):
    service = _service(tmp_path)
    _apply(service, numerator=3, quantity_after=300)
    sold = 0
    for i, quantity in enumerate(chunks):
        _sell(service, quantity, f"sale-{i}")
        sold += quantity
        state = _state(service)
        cost = 100000 * sold // 300
        assert state.realized_gross_pnl_yen == sold * 550 - cost
        assert sum(lot.cost_yen for lot in state.lots["1234"]) == 100000 - cost
    assert state.available_cash_yen == 1065000
    assert state.realized_gross_pnl_yen == 65000


def test_consolidation_keeps_fractional_fifo_pieces_and_records_odd_lot_facts(tmp_path):
    service = _service(tmp_path, lots=3)
    _apply(service, numerator=1, denominator=3, quantity_before=300, quantity_after=100)
    assert [lot.quantity for lot in _state(service).lots["1234"]] == [Fraction(100, 3)] * 3
    _sell(service, 50, "first")
    state = _state(service)
    assert sum(lot.quantity for lot in state.lots["1234"]) == 50
    assert sum(lot.cost_yen for lot in state.lots["1234"]) == 150250
    _sell(service, 50, "second")
    assert _state(service).lots["1234"] == []
    assert _state(service).realized_gross_pnl_yen == 55000 - 300300


@pytest.mark.parametrize("when", ["2026-10-02T00:00:00+09:00", "2026-10-02T14:00:00+09:00"])
def test_late_conversion_precedes_existing_same_day_sale_without_rewriting_it(tmp_path, when):
    service = _service(tmp_path)
    _sell(service, 100, "previous-report", when=datetime.fromisoformat(when))
    before = service.load().events
    _apply(service)
    state = _state(service)
    assert sum(lot.quantity for lot in state.lots["1234"]) == 100
    assert state.realized_gross_pnl_yen == 5000
    assert all(event in service.load().events for event in before)
    assert holding_episode(service.load(), "1234", NOW)[0].event_id == "buy-0"


def test_retry_normalization_and_conflicts_do_not_apply_twice(tmp_path):
    service = _service(tmp_path)
    _apply(service)
    before = (tmp_path / "app.sqlite").read_bytes()
    assert _draft(service, numerator=4, denominator=2, confirmed_at=NOW + timedelta(days=1)) is None
    with pytest.raises(ValueError, match="different content"):
        _draft(service, numerator=3, quantity_after=300)
    with pytest.raises(ValueError, match="already recorded"):
        _draft(service, action_id="same-action-new-id", quantity_before=200, quantity_after=400)
    assert (tmp_path / "app.sqlite").read_bytes() == before


def test_stale_or_unconfirmed_draft_does_not_write(tmp_path):
    service = _service(tmp_path)
    draft = _draft(service)
    before = (tmp_path / "app.sqlite").read_bytes()
    with pytest.raises(ValueError, match="confirmation"):
        apply_draft(service, draft, human_confirmed=False)
    assert (tmp_path / "app.sqlite").read_bytes() == before
    _apply(service)
    before = (tmp_path / "app.sqlite").read_bytes()
    with pytest.raises(ValueError, match="stale"):
        apply_draft(service, draft, human_confirmed=True)
    assert (tmp_path / "app.sqlite").read_bytes() == before


@pytest.mark.parametrize(
    ("ticker", "released", "blocked"),
    [("1234", False, True), ("1234", True, False), ("5678", False, False)],
)
def test_orders_are_not_cancelled_or_rescaled_implicitly(tmp_path, ticker, released, blocked):
    raw = _ledger().model_dump(mode="json")
    raw["events"].append(
        dict(
            type="reservation",
            event_id="order",
            reservation_id="order",
            order_id="broker-order",
            occurred_at="2026-10-01T12:00:00+09:00",
            ticker=ticker,
            side="buy",
            quantity=100,
            price_guard_yen=1000,
            sector="synthetic",
        )
    )
    if released:
        raw["events"].append(
            dict(
                type="release",
                event_id="cancel",
                reservation_id="order",
                reason="cancelled",
                occurred_at="2026-10-01T13:00:00+09:00",
            )
        )
    service = _service(tmp_path, document=PortfolioLedgerDocument.model_validate(raw))
    before = (tmp_path / "app.sqlite").read_bytes()
    if blocked:
        with pytest.raises(ValueError, match="unresolved order"):
            _draft(service)
        assert (tmp_path / "app.sqlite").read_bytes() == before
    else:
        previous = _state(service)
        _apply(service)
        assert _state(service).active_reservations == previous.active_reservations
        assert _state(service).reserved_cash_yen == previous.reserved_cash_yen


@pytest.mark.parametrize(
    "overrides",
    [
        dict(quantity_before=200, quantity_after=400),
        dict(numerator=1, denominator=3, quantity_after=33),
        dict(numerator=0),
        dict(numerator=1),
        dict(confirmed_at=datetime.fromisoformat("2026-10-01T18:00:00+09:00")),
    ],
)
def test_unsupported_or_inconsistent_report_is_no_write(tmp_path, overrides):
    service = _service(tmp_path)
    before = (tmp_path / "app.sqlite").read_bytes()
    with pytest.raises(ValueError, match=r"share conversion|confirmed share"):
        _draft(service, **overrides)
    assert (tmp_path / "app.sqlite").read_bytes() == before


def _market(tmp_path):
    path = tmp_path / "market.sqlite"
    seed_daily_bars(
        path,
        [
            ("1234", "2026-10-01", 1000, 1),
            ("1234", "2026-10-02", 500, 0.5),
            ("1234", "2026-10-05", 500, 1),
        ],
    )
    _seed_calendar(path, date(2026, 10, 1), date(2026, 10, 5))
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE jquants_daily_bars SET ex_right_type='1' WHERE traded_at='2026-10-02'"
        )
    return path


def test_confirmed_conversion_connects_portfolio_and_new_proposal_inputs(tmp_path):
    service = _service(tmp_path)
    market = _market(tmp_path)
    assert current_portfolio(service.load(), sqlite_path=market, now=NOW).total_capital_yen is None
    _apply(service)
    portfolio = current_portfolio(service.load(), sqlite_path=market, now=NOW)
    assert portfolio.total_capital_yen == 1000000
    assert portfolio.holdings[0].quantity == 200
    with closing(connect_read_only(tmp_path / "app.sqlite")) as connection:
        connection.execute("BEGIN")
        inputs = collect_proposal_inputs(
            connection,
            tickers=(),
            proposal_id="",
            now=NOW,
            sqlite_path=market,
            budget_min_yen=200000,
            budget_max_yen=300000,
        )
    assert inputs.positions[0].quantity == 200
    assert inputs.positions[0].quantity_basis_confirmed
    assert inputs.positions[0].cost_yen == 100000
    assert inputs.total_capital_yen == 1000000
    _sell(service, 200, "full-sale")
    assert (
        current_portfolio(service.load(), sqlite_path=market, now=NOW).total_capital_yen == 1010000
    )


@pytest.mark.parametrize(
    "damage",
    [
        "different-ratio",
        "unit-factor",
        "unknown-kind",
        "missing-factor",
        "missing-row",
        "new-action",
        "old-quote",
    ],
)
def test_confirmation_does_not_hide_other_basis_or_price_gaps(tmp_path, damage):
    service = _service(tmp_path)
    market = _market(tmp_path)
    _apply(service)
    with sqlite3.connect(market) as c:
        if damage == "different-ratio":
            c.execute(
                "UPDATE jquants_daily_bars SET adjustment_factor=.25 WHERE traded_at='2026-10-02'"
            )
        elif damage == "unit-factor":
            c.execute(
                "UPDATE jquants_daily_bars SET adjustment_factor=1 WHERE traded_at='2026-10-02'"
            )
        elif damage == "unknown-kind":
            c.execute(
                "UPDATE jquants_daily_bars SET ex_right_type=NULL WHERE traded_at='2026-10-02'"
            )
        elif damage == "missing-factor":
            c.execute(
                "UPDATE jquants_daily_bars SET adjustment_factor=NULL WHERE traded_at='2026-10-02'"
            )
        elif damage == "missing-row":
            c.execute("DELETE FROM jquants_daily_bars WHERE traded_at='2026-10-02'")
        elif damage == "new-action":
            c.execute(
                "UPDATE jquants_daily_bars SET adjustment_factor=.5,ex_right_type='1' WHERE traded_at='2026-10-05'"
            )
        else:
            c.execute("DELETE FROM jquants_daily_bars WHERE traded_at>'2026-10-01'")
    snapshot = current_portfolio(service.load(), sqlite_path=market, now=NOW)
    assert snapshot.total_capital_yen is None
    assert snapshot.holdings[0].quantity == 200
    assert snapshot.available_cash_yen == 900000


@pytest.mark.parametrize("received_factor", [0.5, 1.0])
def test_conversion_is_not_external_flow_and_preserves_twr(tmp_path, received_factor):
    raw = _ledger().model_dump(mode="json")
    for event in raw["events"]:
        event["occurred_at"] = event["occurred_at"].replace("2026-10-01", "2025-01-06")
    service = _service(tmp_path, document=PortfolioLedgerDocument.model_validate(raw))
    document = _apply(service, basis_date=date(2026, 1, 5))
    bars = tuple(
        JQuantsDailyBar("1234", day, price, None, adjustment_factor=factor, ex_right_type=kind)
        for day, price, factor, kind in [
            (date(2025, 1, 6), 1000, 1, None),
            (date(2026, 1, 5), 500, received_factor, "1"),
            (date(2026, 1, 6), 500, 1, None),
        ]
    )
    result = compute_portfolio_outcome(
        document, _benchmark(), business_days=tuple(bar.traded_at for bar in bars), bars=bars
    )
    if received_factor != 0.5:
        assert result.status == "unresolved"
        assert result.reason == "corporate_action_unresolved"
        return
    assert result.status == "resolved"
    assert result.portfolio_twr_pct == 0
    assert result.ending_holdings_market_value_yen == 100000
    assert result.ending_cash_yen == 900000


def test_cli_only_creates_draft_until_confirmed(tmp_path, capsys):
    service = _service(tmp_path)
    before = (tmp_path / "app.sqlite").read_bytes()
    out = tmp_path / "draft.yaml"
    args = [
        "share-conversion-draft",
        "--db",
        str(tmp_path / "app.sqlite"),
        "--action-id",
        "split",
        "--ticker",
        "1234",
        "--basis-date",
        str(BASIS),
        "--numerator",
        "2",
        "--denominator",
        "1",
        "--quantity-before",
        "100",
        "--quantity-after",
        "200",
        "--confirmed-at",
        NOW.isoformat(),
        "--source-ref",
        "synthetic:human",
        "--out",
        str(out),
    ]
    assert main(args) == 0
    assert (tmp_path / "app.sqlite").read_bytes() == before
    assert load_draft(out).kind == "share-conversion"
    assert main(["apply-draft", str(out), "--db", str(tmp_path / "app.sqlite")]) == 2
    assert (tmp_path / "app.sqlite").read_bytes() == before
    assert main(["apply-draft", str(out), "--db", str(tmp_path / "app.sqlite"), "--confirmed"]) == 0
    assert sum(lot.quantity for lot in _state(service).lots["1234"]) == 200


def test_unconverted_fractional_yen_sale_keeps_old_rejection():
    raw = _ledger().model_dump(mode="json")
    raw["events"][1].update(quantity=1000, price_yen="10.123")
    raw["events"].append(
        dict(
            type="execution",
            event_id="sell",
            execution_id="sell",
            ticker="1234",
            side="sell",
            occurred_at=NOW.isoformat(),
            quantity=100,
            price_yen=11,
        )
    )
    document = PortfolioLedgerDocument.model_validate(raw)
    with pytest.raises(PortfolioLedgerError, match="sell FIFO cost"):
        replay_events_through(document.events, NOW)


def test_two_distinct_actions_and_partial_sale_keep_fifo_identity(tmp_path):
    service = _service(tmp_path)
    _apply(service, numerator=3, quantity_after=300)
    _sell(service, 100, "partial")
    _apply(
        service,
        action_id="reverse",
        basis_date=date(2026, 10, 6),
        numerator=1,
        denominator=2,
        quantity_before=200,
        quantity_after=100,
        confirmed_at=NOW + timedelta(days=1),
    )
    state = _state(service)
    assert sum(lot.cost_yen for lot in state.lots["1234"]) == 66667
    assert sum(lot.quantity for lot in state.lots["1234"]) == 100
    _sell(service, 100, "exit", when=NOW + timedelta(days=1))
    assert _state(service).realized_gross_pnl_yen == 10000


def test_closed_position_keeps_received_contradiction_after_confirmed_conversion(tmp_path):
    from baibai_engine.portfolio.holdings.valuation import UnresolvedHoldingQuantityError

    service = _service(tmp_path)
    market = _market(tmp_path)
    _apply(service)
    _sell(service, 200, "full-sale")
    with sqlite3.connect(market) as connection:
        connection.execute(
            "UPDATE jquants_daily_bars SET adjustment_factor=1 WHERE traded_at='2026-10-02'"
        )
    with pytest.raises(UnresolvedHoldingQuantityError, match="1234"):
        current_portfolio(service.load(), sqlite_path=market, now=NOW)
