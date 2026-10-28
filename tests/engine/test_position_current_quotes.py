"""価格非依存の台帳事実、override、株式basis、現在NAVの回帰。"""

import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger
from tests.helpers.screening_sqlite import seed_daily_bars

from baibai_engine.portfolio.ledger.drafts import apply_draft, build_event_draft, build_sell_execution_draft
from baibai_engine.portfolio.ledger.events import replay_events_through
from baibai_engine.portfolio.ledger.store import LedgerStoreService

NOW = datetime.fromisoformat("2026-09-07T18:00:00+09:00")


@pytest.fixture
def holding_case(tmp_path):
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
    return db, market, ledger


@pytest.mark.parametrize(
    ("kind", "details", "delta"),
    [
        ("contribution", {}, 1000),
        ("income", {"ticker": "2331", "income_kind": "dividend"}, 1000),
        ("cost", {"ticker": "2331", "cost_kind": "commission"}, -1000),
        ("tax_confirmed", {"ticker": "2331", "tax_kind": "dividend"}, -1000),
        ("withdrawal", {}, -1000),
    ],
)
def test_human_event_draft_apply_needs_no_holding_price(holding_case, kind, details, delta):
    from pydantic import TypeAdapter

    from baibai_engine.portfolio.ledger.drafts import HumanEvent
    from baibai_engine.portfolio.ledger.events import PortfolioLedgerError

    db, _, _ = holding_case
    service = LedgerStoreService(db)
    before = replay_events_through(service.load().events, NOW)
    event = TypeAdapter(HumanEvent).validate_python(
        dict(
            type=kind,
            event_id="human-event",
            occurred_at=NOW,
            amount_yen=1000,
            **details,
        )
    )
    draft = build_event_draft(service, event)
    apply_draft(service, draft, human_confirmed=True)
    assert "market_prices" not in service.load().model_dump()
    after = replay_events_through(service.load().events, NOW)
    assert after.available_cash_yen == before.available_cash_yen + delta
    if delta < 0:
        with pytest.raises(PortfolioLedgerError):
            build_event_draft(
                service,
                event.model_copy(
                    update={"event_id": "overdraw", "amount_yen": after.available_cash_yen + 1}
                ),
            )
    with pytest.raises(PortfolioLedgerError, match="exceeds"):
        build_sell_execution_draft(
            service, occurred_at=NOW, ticker="2331", quantity=300, price_yen=Decimal(1000)
        )


def test_override_constraints_are_price_independent_and_expiry_uses_valuation_instant(holding_case):
    from baibai_engine.portfolio.ledger.drafts import build_override_draft
    from baibai_engine.portfolio.ledger.events import HumanOverride, PortfolioLedgerError
    from baibai_engine.portfolio.holdings.valuation import current_portfolio

    db, market, ledger = holding_case
    service = LedgerStoreService(db)
    accepted = HumanOverride.model_validate(
        dict(
            override_id="accepted",
            scope="ticker",
            key="2331",
            reason="一時的な集中を確認",
            decision_reference="human-confirmation",
            approved_at=ledger.as_of,
            expires_at=ledger.as_of + timedelta(days=1),
        )
    )
    too_long = accepted.model_copy(update={"expires_at": ledger.as_of + timedelta(days=32)})
    before = service.load()
    with pytest.raises(PortfolioLedgerError, match="exceeds 31 days"):
        build_override_draft(service, too_long)
    valid = build_override_draft(service, accepted)
    # Hand-editing a draft cannot bypass the same price-independent apply boundary.
    tampered = valid.model_copy(
        update={"replacement": valid.replacement.model_copy(update={"overrides": (too_long,)})}
    )
    with pytest.raises(PortfolioLedgerError, match="exceeds 31 days"):
        apply_draft(service, tampered, human_confirmed=True)
    assert service.load() == before
    apply_draft(service, valid, human_confirmed=True)
    with pytest.raises(PortfolioLedgerError, match="multiple active overrides"):
        build_override_draft(service, accepted.model_copy(update={"override_id": "duplicate"}))
    with closing(sqlite3.connect(market)) as connection, connection:
        connection.execute("UPDATE jquants_daily_bars SET close=10000 WHERE traded_at='2026-09-04'")
    snapshot = current_portfolio(service.load(), sqlite_path=market, now=NOW)
    warning = next(
        item for item in snapshot.warnings if item.scope == "ticker" and item.key == "2331"
    )
    assert warning.actual_pct > 10
    assert not warning.overridden
    assert snapshot.as_of == NOW
    assert service.load().as_of == ledger.as_of


@pytest.mark.parametrize("factor", [1.0, 0.5, None])
def test_intraday_fill_must_bridge_quote_and_quantity_dates(tmp_path, factor):
    from tests.helpers.screening_sqlite import seed_daily_bars

    from baibai_engine.portfolio.ledger.events import PortfolioLedgerDocument
    from baibai_engine.portfolio.holdings.valuation import current_portfolio, holding_quote

    now = NOW.replace(hour=10)
    ledger = PortfolioLedgerDocument.model_validate(
        dict(
            schema_version=3,
            portfolio_scope="repository_only",
            as_of=now.isoformat(),
            events=[
                dict(
                    type="opening_balance",
                    event_id="opening",
                    occurred_at="2026-09-07T08:00:00+09:00",
                    amount_yen=1000000,
                ),
                dict(
                    type="reservation",
                    event_id="reserve",
                    occurred_at="2026-09-07T09:00:00+09:00",
                    reservation_id="order",
                    order_id="order",
                    ticker="2331",
                    sector="test",
                    common_factors=[],
                    quantity=100,
                    price_guard_yen=500,
                    expires_at="2026-09-07T15:30:00+09:00",
                ),
                dict(
                    type="execution",
                    event_id="fill",
                    occurred_at="2026-09-07T09:01:00+09:00",
                    execution_id="fill",
                    reservation_id="order",
                    ticker="2331",
                    side="buy",
                    quantity=100,
                    price_yen=500,
                ),
            ],
        )
    )
    market = tmp_path / "market.sqlite"
    seed_daily_bars(market, [("2331", "2026-09-04", 1000, 1), ("2331", "2026-09-07", None, factor)])
    quote, confirmed = holding_quote(ledger, ticker="2331", sqlite_path=market, now=now)
    assert quote.price_as_of.isoformat() == "2026-09-04"
    assert confirmed is (factor == 1.0)
    snapshot = current_portfolio(ledger, sqlite_path=market, now=now)
    assert snapshot.available_cash_yen == 950000
    assert snapshot.holdings[0].quantity == 100
    assert snapshot.holdings[0].deployed_cost_yen == 50000
    assert snapshot.total_capital_yen == (1050000 if factor == 1.0 else None)


def test_partial_sale_waits_for_session_then_restores_valuation(holding_case):
    from tests.helpers.screening_sqlite import seed_daily_bars

    from baibai_engine.portfolio.holdings.valuation import current_portfolio, holding_quote

    db, market, _ = holding_case
    service = LedgerStoreService(db)
    sell = build_sell_execution_draft(
        service,
        occurred_at=NOW,
        ticker="2331",
        quantity=100,
        price_yen=Decimal(1000),
        decision_reference="human-sale-report",
    )
    apply_draft(service, sell, human_confirmed=True)
    ledger = service.load()
    old_quote, confirmed = holding_quote(ledger, ticker="2331", sqlite_path=market, now=NOW)
    assert old_quote is not None
    assert old_quote.price_as_of == date(2026, 9, 4)
    assert not confirmed
    before = current_portfolio(ledger, sqlite_path=market, now=NOW)
    assert before.holdings[0].quantity == 100
    assert before.holdings[0].market_value_yen is None
    assert before.total_capital_yen is None

    seed_daily_bars(market, [("2331", "2026-09-07", 1000.0, 1.0)])
    current_quote, confirmed = holding_quote(ledger, ticker="2331", sqlite_path=market, now=NOW)
    assert current_quote is not None
    assert current_quote.price_as_of == date(2026, 9, 7)
    assert confirmed
    after = current_portfolio(ledger, sqlite_path=market, now=NOW)
    assert after.holdings[0].quantity == 100
    assert after.holdings[0].market_value_yen == 100000
    assert after.total_capital_yen == (
        after.available_cash_yen + after.reserved_cash_yen + after.holdings_market_value_yen
    )


def test_current_portfolio_leaves_stale_market_quotes_unvalued(holding_case):
    from baibai_engine.portfolio.holdings.valuation import current_portfolio

    _, market, ledger = holding_case
    valuation_at = NOW + timedelta(days=30)
    snapshot = current_portfolio(ledger, sqlite_path=market, now=valuation_at)
    assert snapshot.as_of == valuation_at
    assert snapshot.available_cash_yen == 10080500
    assert snapshot.holdings[0].quantity == 200
    assert snapshot.holdings[0].market_value_yen is None
    assert snapshot.total_capital_yen is None


@pytest.mark.parametrize("valuation_at", [NOW - timedelta(days=1), NOW + timedelta(days=1)])
def test_portfolio_summary_uses_the_replayed_instant(holding_case, valuation_at):
    from baibai_engine.portfolio.ledger.events import summarize_portfolio

    _, _, ledger = holding_case
    recorded_at = ledger.as_of
    state = replay_events_through(ledger.events, valuation_at)
    snapshot = summarize_portfolio(ledger, state, {})
    assert snapshot.as_of == valuation_at
    assert snapshot.ledger_as_of == recorded_at
    assert snapshot.available_cash_yen == state.available_cash_yen
    assert ledger.as_of == recorded_at


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        ("2026-10-14T15:29:59+09:00", "2026-10-13"),
        ("2026-10-14T15:30:00+09:00", "2026-10-14"),
        ("2026-10-14T06:30:00Z", "2026-10-14"),
    ],
)
def test_close_date_cutoff_is_not_a_source_observation_timestamp(instant, expected):
    from baibai_engine.machine_analysis.observations.prices.sessions import latest_eligible_close_date

    assert latest_eligible_close_date(datetime.fromisoformat(instant)).isoformat() == expected


@pytest.mark.parametrize("approval_offset_days", [-1, 0, 1])
def test_override_report_roundtrips_and_advances_only_its_metadata_cutoff(
    holding_case, tmp_path, approval_offset_days
):
    from baibai_engine.portfolio.ledger.drafts import build_override_draft, load_draft, write_draft
    from baibai_engine.portfolio.ledger.events import HumanOverride, active_human_overrides

    db, _, ledger = holding_case
    service = LedgerStoreService(db)
    approved = ledger.as_of + timedelta(days=approval_offset_days)
    override = HumanOverride(
        override_id="later-human-approval",
        scope="ticker",
        key="2331",
        reason="確認済みの集中を一時許容",
        decision_reference="human-confirmation",
        approved_at=approved,
        expires_at=approved + timedelta(days=2),
    )
    before_head = service.append_head()
    draft = build_override_draft(service, override)
    path = tmp_path / "override-draft.yaml"
    write_draft(path, draft)
    restored = load_draft(path)
    assert restored == draft
    assert service.load() == ledger
    apply_draft(service, restored, human_confirmed=True)
    saved = service.load()
    assert saved.as_of == max(ledger.as_of, approved)
    assert saved.events == ledger.events
    assert service.append_head() == before_head
    assert saved.overrides == (override,)
    assert (
        active_human_overrides(saved.overrides, as_of=saved.as_of)[("ticker", "2331")] == override
    )


@pytest.mark.parametrize(
    ("offset", "first_index", "last_index"),
    [(0, None, None), (1, 0, 0), (2, 0, 1), (3, None, 2), (4, 3, 3)],
)
def test_holding_episode_tracks_partial_sale_full_exit_and_reentry(offset, first_index, last_index):
    from baibai_engine.portfolio.ledger.events import (
        ExecutionEvent,
        OpeningBalanceEvent,
        PortfolioLedgerDocument,
    )
    from baibai_engine.portfolio.holdings.valuation import holding_episode

    trades = tuple(
        ExecutionEvent(
            type="execution",
            event_id=f"trade-{index}",
            execution_id=f"fill-{index}",
            occurred_at=NOW + timedelta(days=index + 1),
            ticker="2331",
            side=side,
            quantity=quantity,
            price_yen=Decimal(1000),
            sector="test",
        )
        for index, (side, quantity) in enumerate(
            [("buy", 200), ("sell", 100), ("sell", 100), ("buy", 100)]
        )
    )
    ledger = PortfolioLedgerDocument(
        schema_version=3,
        portfolio_scope="repository_only",
        as_of=NOW + timedelta(days=4),
        events=(
            OpeningBalanceEvent(
                type="opening_balance", event_id="cash", occurred_at=NOW, amount_yen=1000000
            ),
            *trades,
        ),
    )
    assert holding_episode(ledger, "2331", NOW + timedelta(days=offset)) == (
        None if first_index is None else trades[first_index],
        None if last_index is None else trades[last_index].occurred_at,
    )
    assert holding_episode(ledger, "9999", ledger.as_of) == (None, None)
