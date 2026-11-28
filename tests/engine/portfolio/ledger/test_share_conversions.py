"""Confirmed whole-share conversions preserve capital, order facts and frozen history."""

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction

import pytest
import yaml
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
from baibai_engine.portfolio.performance.calculate import (
    compute_portfolio_outcome,
    outcome_to_payload,
)
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_web.readmodel.builders import dashboard
from baibai_web.sources.db_sources import DbLedgerSource, DbMarketSource

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
    args.setdefault("now", args["confirmed_at"])
    return build_share_conversion_draft(service, **args)


def _apply(service, **overrides):
    draft = _draft(service, **overrides)
    assert draft is not None
    apply_draft(service, draft, human_confirmed=True, now=draft.replacement.as_of)
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
            current_listing=None,
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


@pytest.mark.parametrize(
    ("second_quote", "total_mark", "emitted_mark"),
    [("400.1", "20005.0", 20005), ("400.2", "20007.5", 20008)],
)
def test_reference_marks_keep_sub_yen_precision_until_whole_yen_output(
    tmp_path, mocker, second_quote, total_mark, emitted_mark
):
    raw = _ledger().model_dump(mode="json")
    for event in raw["events"]:
        event["occurred_at"] = event["occurred_at"].replace("2026-10-01", "2025-01-06")
    raw["events"][1]["price_yen"] = "100"
    raw["events"].append(
        dict(raw["events"][1], event_id="other-buy", execution_id="other-buy", ticker="5678")
    )
    app = _service(tmp_path, document=PortfolioLedgerDocument.model_validate(raw))
    basis = date(2026, 1, 5)
    confirmation = datetime.fromisoformat("2026-01-05T17:00:00+09:00")
    changes = dict(
        basis_date=basis, numerator=1, denominator=4, quantity_after=25, confirmed_at=confirmation
    )
    _apply(app, **changes)
    ledger = _apply(app, **changes, ticker="5678", action_id="synthetic-other-consolidation")
    benchmark = _benchmark()
    start, end = benchmark.period_start_date, benchmark.period_end_date
    market = tmp_path / "market.sqlite"
    days = tuple(start + timedelta(days=i) for i in range((end - start).days + 1))
    business_days = tuple(day for day in days if day.weekday() < 5)
    bars = tuple(
        JQuantsDailyBar(
            ticker,
            day,
            float(price) if day == end else 400.0 if day == basis else 100.0,
            None,
            adjustment_factor=4.0 if day == basis else 1.0,
            ex_right_type="2" if day == basis else None,
        )
        for ticker, price in (("1234", "400.1"), ("5678", second_quote))
        for day in business_days
    )
    seed_daily_bars(
        market, [(b.ticker, b.traded_at.isoformat(), b.close, b.adjustment_factor) for b in bars]
    )
    _seed_calendar(market, start, end)
    with closing(sqlite3.connect(market)) as connection, connection:
        connection.execute(
            "UPDATE jquants_daily_bars SET ex_right_type='2' WHERE traded_at=?",
            (basis.isoformat(),),
        )
    at = datetime.fromisoformat("2026-01-06T16:00:00+09:00")
    before = (tmp_path / "app.sqlite").read_bytes(), market.read_bytes()
    portfolio = current_portfolio(ledger, sqlite_path=market, now=at)
    assert portfolio.available_cash_yen == 980000
    assert portfolio.deployed_cost_yen == 20000
    assert portfolio.holdings[0].market_value_yen == Decimal("10002.5")
    assert portfolio.holdings_market_value_yen == Decimal(total_mark)
    assert portfolio.total_capital_yen == 980000 + Decimal(total_mark)

    mocker.patch.object(dashboard, "datetime").now.return_value = at
    view = dashboard.build_dashboard(
        DbLedgerSource(tmp_path / "app.sqlite", market), DbMarketSource(market)
    )
    assert view.valuation_error is None
    assert view.valuation_as_of == end
    assert view.total_capital_yen == 980000 + emitted_mark
    assert view.holdings_market_value_yen == emitted_mark
    held = {holding.ticker: holding for holding in view.holdings}
    assert held["1234"].market_price_yen == "400.1"
    assert held["1234"].market_value_yen == 10003
    assert held["1234"].unrealized_pnl_yen == 3

    outcome = compute_portfolio_outcome(ledger, benchmark, business_days=business_days, bars=bars)
    assert outcome.status == "resolved"
    assert outcome.portfolio_twr_pct == pytest.approx(
        float((Decimal(total_mark) - 20000) / 10_000), abs=1e-10
    )
    assert outcome_to_payload(outcome)["ending_holdings_market_value_yen"] == emitted_mark
    assert type(outcome_to_payload(outcome)["ending_holdings_market_value_yen"]) is int
    with pytest.raises(PortfolioLedgerError, match="sell execution notional must reconcile"):
        build_sell_execution_draft(
            app, occurred_at=at, now=at, ticker="1234", quantity=25, price_yen=Decimal("400.1")
        )
    assert ((tmp_path / "app.sqlite").read_bytes(), market.read_bytes()) == before


def _share_conversion_command(db, out, *, basis_date=BASIS, confirmed_at=NOW):
    return [
        "share-conversion-draft",
        "--db",
        str(db),
        "--action-id",
        "split",
        "--ticker",
        "1234",
        "--basis-date",
        str(basis_date),
        "--numerator",
        "2",
        "--denominator",
        "1",
        "--quantity-before",
        "100",
        "--quantity-after",
        "200",
        "--confirmed-at",
        confirmed_at.isoformat(),
        "--source-ref",
        "synthetic:human",
        "--out",
        str(out),
    ]


def test_cli_only_creates_draft_until_confirmed(tmp_path, capsys):
    service = _service(tmp_path)
    db, out = tmp_path / "app.sqlite", tmp_path / "draft.yaml"
    before = db.read_bytes()
    assert main(_share_conversion_command(db, out), now=NOW) == 0
    assert db.read_bytes() == before
    assert load_draft(out).kind == "share-conversion"
    apply = ["apply-draft", str(out), "--db", str(db)]
    assert main(apply, now=NOW) == 2
    assert db.read_bytes() == before
    assert main([*apply, "--confirmed"], now=NOW) == 0
    assert sum(lot.quantity for lot in _state(service).lots["1234"]) == 200


@pytest.mark.parametrize(
    ("basis_date", "confirmation", "accepted"),
    [
        pytest.param(BASIS, NOW - timedelta(days=1), True, id="late-report"),
        pytest.param(BASIS, NOW.astimezone(UTC), True, id="same-instant-offset"),
        pytest.param(BASIS, NOW + timedelta(microseconds=1), False, id="same-day-future"),
        pytest.param(
            NOW.date() + timedelta(days=1), NOW + timedelta(days=1), False, id="scheduled"
        ),
    ],
)
@pytest.mark.parametrize("entry", ["draft", "apply"])
def test_share_conversion_requires_an_effective_confirmation_at_draft_and_apply(
    tmp_path, capsys, basis_date, confirmation, accepted, entry
):
    service = _service(tmp_path)
    db, out = tmp_path / "app.sqlite", tmp_path / "draft.yaml"
    before = db.read_bytes()
    command = _share_conversion_command(db, out, basis_date=basis_date, confirmed_at=confirmation)
    if entry == "draft":
        assert main(command, now=NOW) == (0 if accepted else 2)
        assert out.exists() is accepted
        assert db.read_bytes() == before
        if not accepted:
            assert "confirmed_at must not be in the future" in capsys.readouterr().err
        return

    # A previously prepared or separately authored draft is still checked at apply.
    assert main(command, now=max(NOW, confirmation)) == 0
    capsys.readouterr()
    assert db.read_bytes() == before
    draft_bytes = out.read_bytes()
    apply = ["apply-draft", str(out), "--db", str(db), "--confirmed"]
    assert main(apply, now=NOW) == (0 if accepted else 2)
    if not accepted:
        assert "confirmed_at must not be in the future" in capsys.readouterr().err
        assert db.read_bytes() == before
        assert main(apply, now=confirmation) == 0
    assert sum(lot.quantity for lot in _state(service).lots["1234"]) == 200
    assert service.load().events[-1].confirmed_at == confirmation
    assert out.read_bytes() == draft_bytes


def test_exact_recorded_conversion_retry_ignores_the_current_report_clock(tmp_path, capsys):
    _service(tmp_path)
    db, out = tmp_path / "app.sqlite", tmp_path / "draft.yaml"
    assert main(_share_conversion_command(db, out), now=NOW) == 0
    assert main(["apply-draft", str(out), "--db", str(db), "--confirmed"], now=NOW) == 0
    capsys.readouterr()
    before = db.read_bytes()
    retry = tmp_path / "retry.yaml"
    assert main(_share_conversion_command(db, retry), now=NOW - timedelta(microseconds=1)) == 0
    assert yaml.safe_load(capsys.readouterr().out) == {"status": "no_change"}
    assert not retry.exists()
    assert db.read_bytes() == before


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


def test_share_conversion_never_rewrites_the_original_proposal(tmp_path):
    from tests.helpers.proposal import (
        PUBLISHED,
        backend_case,
        read_saved_proposal,
        reviewed_proposal,
    )

    from baibai_engine.investment_judgment.trade_proposal.contracts import proposal_draft_sha256
    from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService

    case = backend_case(tmp_path, holding=True)
    saved = TradeProposalService(
        case.app,
        listing_reader=case.listing_reader,
        sqlite_path=case.market,
        clock=lambda: PUBLISHED,
    ).publish(reviewed_proposal(case, action="hold"))
    original = saved.model_dump(mode="json")
    original_sha = proposal_draft_sha256(saved)
    # A subsequent human-confirmed unit change must not restate the terms in a saved decision.
    updated = _apply(
        LedgerStoreService(case.app),
        basis_date=date(2026, 10, 16),
        confirmed_at=datetime.fromisoformat("2026-10-16T18:00:00+09:00"),
    )
    assert updated.schema_version == 4
    recovered = read_saved_proposal(proposal_id=saved.proposal_id, db_path=case.app)
    assert recovered.model_dump(mode="json") == original
    assert proposal_draft_sha256(recovered) == original_sha
