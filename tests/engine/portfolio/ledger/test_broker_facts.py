from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import pytest
from tests.helpers.ledger import load_portfolio_ledger, portfolio_snapshot

from baibai_engine.portfolio.ledger.broker_facts import (
    BrokerFactRecordingError,
    record_broker_fact,
)
from baibai_engine.portfolio.ledger.events import (
    ExecutionEvent,
    ReservationEvent,
    replay_events_through,
    reservation_snapshots,
)

FIXTURE = Path(__file__).parents[3] / "fixtures" / "portfolio-ledger" / "representative.yaml"
JST = ZoneInfo("Asia/Tokyo")
PROPOSAL = "https://github.com/koumatsumoto/baibai-loop/issues/360#issuecomment-1"


def _at(hour: int) -> datetime:
    return datetime(2026, 7, 12, hour, 0, tzinfo=JST)


def test_open_report_creates_a_source_bound_reservation() -> None:
    result = record_broker_fact(
        load_portfolio_ledger(FIXTURE),
        decision_reference=PROPOSAL,
        status="open",
        occurred_at=_at(9),
        ticker="1234",
        quantity=100,
        sector="サービス業",
        price_guard_yen=Decimal("900"),
        expires_at=datetime(2026, 7, 20, 15, 30, tzinfo=JST),
        now=_at(12),
    )
    assert result.changed is True
    reservation = portfolio_snapshot(result.document).active_reservations[-1]
    assert reservation.ticker == "1234"
    assert result.document.events[-1].decision_reference == PROPOSAL


def test_appending_broker_fact_preserves_wire_validation_of_existing_events() -> None:
    source = load_portfolio_ledger(FIXTURE)
    saved_fill = next(event for event in source.events if isinstance(event, ExecutionEvent))
    # Typed arithmetic accepts this Decimal, but it is not a supported price on the ledger wire.
    typed_fill = ExecutionEvent.model_validate(
        {**saved_fill.model_dump(mode="python"), "price_yen": Decimal("1E+3")}
    )
    source = source.model_copy(
        update={
            "events": tuple(typed_fill if event is saved_fill else event for event in source.events)
        }
    )
    with pytest.raises(
        BrokerFactRecordingError, match="string unit price must use fixed-point decimal notation"
    ):
        record_broker_fact(
            source,
            decision_reference=PROPOSAL,
            status="open",
            occurred_at=_at(9),
            ticker="1234",
            quantity=100,
            sector="サービス業",
            price_guard_yen=Decimal("900"),
            now=_at(12),
        )


def test_sell_order_and_partial_fill_leave_cash_and_unfilled_shares_distinct() -> None:
    original = load_portfolio_ledger(FIXTURE)
    before = portfolio_snapshot(original)
    opened = record_broker_fact(
        original,
        decision_reference="human-report-2331-sell",
        status="open",
        side="sell",
        occurred_at=_at(9),
        ticker="2331",
        quantity=200,
        sector="サービス業",
        price_guard_yen=Decimal("1000"),
        expires_at=datetime(2026, 7, 20, 15, 30, tzinfo=JST),
        now=_at(12),
    )
    open_snapshot = portfolio_snapshot(opened.document)
    sell = next(item for item in open_snapshot.active_reservations if item.side == "sell")
    assert sell.remaining_quantity == 200
    assert sell.reserved_yen == 0
    assert open_snapshot.available_cash_yen == before.available_cash_yen
    assert open_snapshot.reserved_cash_yen == before.reserved_cash_yen
    assert next(item for item in open_snapshot.holdings if item.ticker == "2331").quantity == 200

    repeated = record_broker_fact(
        opened.document,
        decision_reference="human-report-2331-sell",
        status="open",
        side="sell",
        occurred_at=_at(9),
        ticker="2331",
        quantity=200,
        sector="サービス業",
        price_guard_yen=Decimal("1000"),
        expires_at=datetime(2026, 7, 20, 15, 30, tzinfo=JST),
        now=_at(12),
    )
    assert repeated.changed is False

    filled = record_broker_fact(
        opened.document,
        decision_reference="human-report-2331-sell",
        status="filled",
        side="sell",
        occurred_at=_at(10),
        ticker="2331",
        quantity=100,
        price_yen=Decimal("1010"),
        reservation_id=sell.reservation_id,
        now=_at(12),
    )
    after = portfolio_snapshot(filled.document)
    assert (
        next(item for item in after.active_reservations if item.side == "sell").remaining_quantity
        == 100
    )
    assert next(item for item in after.holdings if item.ticker == "2331").quantity == 100
    assert after.available_cash_yen == before.available_cash_yen + 101_000
    assert after.reserved_cash_yen == before.reserved_cash_yen

    cancelled = record_broker_fact(
        filled.document,
        decision_reference="human-report-2331-sell",
        status="cancelled",
        occurred_at=_at(11),
        reservation_id=sell.reservation_id,
        now=_at(12),
    )
    final = portfolio_snapshot(cancelled.document)
    assert all(item.side != "sell" for item in final.active_reservations)
    assert final.available_cash_yen == after.available_cash_yen
    assert next(item for item in final.holdings if item.ticker == "2331").quantity == 100


def test_sell_orders_cannot_commit_more_shares_than_held() -> None:
    opened = record_broker_fact(
        load_portfolio_ledger(FIXTURE),
        decision_reference="human-report-2331-sell",
        status="open",
        side="sell",
        occurred_at=_at(9),
        ticker="2331",
        quantity=200,
        sector="サービス業",
        price_guard_yen=Decimal("1000"),
        expires_at=datetime(2026, 7, 20, 15, 30, tzinfo=JST),
        now=_at(12),
    )
    with pytest.raises(BrokerFactRecordingError, match="exceeds uncommitted holding"):
        record_broker_fact(
            opened.document,
            decision_reference="human-report-2331-second-sell",
            status="open",
            side="sell",
            occurred_at=_at(10),
            ticker="2331",
            quantity=100,
            sector="サービス業",
            price_guard_yen=Decimal("990"),
            expires_at=datetime(2026, 7, 20, 15, 30, tzinfo=JST),
            now=_at(12),
        )

    order_id = next(
        item.reservation_id
        for item in portfolio_snapshot(opened.document).active_reservations
        if item.side == "sell"
    )
    with pytest.raises(BrokerFactRecordingError, match="below limit price"):
        record_broker_fact(
            opened.document,
            decision_reference="human-report-2331-sell",
            status="filled",
            side="sell",
            occurred_at=_at(10),
            ticker="2331",
            quantity=100,
            price_yen=Decimal("990"),
            reservation_id=order_id,
            now=_at(12),
        )


def test_filled_report_consumes_the_matching_active_reservation() -> None:
    opened = record_broker_fact(
        load_portfolio_ledger(FIXTURE),
        decision_reference=PROPOSAL,
        status="open",
        occurred_at=_at(9),
        ticker="2331",
        quantity=100,
        sector="サービス業",
        price_guard_yen=Decimal("1100"),
        expires_at=datetime(2026, 7, 20, 15, 30, tzinfo=JST),
        now=_at(12),
    )
    reservation_id = next(
        item.reservation_id
        for item in portfolio_snapshot(opened.document).active_reservations
        if item.ticker == "2331"
    )
    result = record_broker_fact(
        opened.document,
        decision_reference=PROPOSAL,
        status="filled",
        occurred_at=_at(10),
        ticker="2331",
        quantity=100,
        price_yen=Decimal("1090"),
        reservation_id=reservation_id,
        now=_at(12),
    )
    snapshot = portfolio_snapshot(result.document)
    assert all(item.reservation_id != reservation_id for item in snapshot.active_reservations)
    assert any(item.ticker == "2331" and item.quantity == 300 for item in snapshot.holdings)


def test_cancelled_report_releases_only_the_reported_reservation() -> None:
    result = record_broker_fact(
        load_portfolio_ledger(FIXTURE),
        decision_reference=PROPOSAL,
        status="cancelled",
        occurred_at=_at(10),
        reservation_id="reservation-8929-pending",
        now=_at(12),
    )
    assert (
        reservation_snapshots(replay_events_through(result.document.events, result.document.as_of))
        == ()
    )
    repeated = record_broker_fact(
        result.document,
        decision_reference=PROPOSAL,
        status="cancelled",
        occurred_at=_at(10),
        reservation_id="reservation-8929-pending",
        now=_at(12),
    )
    assert repeated.changed is False


def test_expired_report_checks_known_expiry_and_is_idempotent() -> None:
    ledger = load_portfolio_ledger(FIXTURE)
    expiry = datetime(2026, 7, 31, 15, 30, tzinfo=JST)
    now = datetime(2026, 8, 1, 12, 0, tzinfo=JST)
    with pytest.raises(BrokerFactRecordingError, match="predates expires_at"):
        record_broker_fact(
            ledger,
            decision_reference=PROPOSAL,
            status="expired",
            occurred_at=datetime(2026, 7, 31, 15, 29, tzinfo=JST),
            reservation_id="reservation-8929-pending",
            now=now,
        )

    result = record_broker_fact(
        ledger,
        decision_reference=PROPOSAL,
        status="expired",
        occurred_at=expiry,
        reservation_id="reservation-8929-pending",
        now=now,
    )
    release = result.document.events[-1]
    assert release.type == "release"
    assert release.reason == "expired"
    assert (
        reservation_snapshots(replay_events_through(result.document.events, result.document.as_of))
        == ()
    )
    repeated = record_broker_fact(
        result.document,
        decision_reference=PROPOSAL,
        status="expired",
        occurred_at=expiry,
        reservation_id="reservation-8929-pending",
        now=now,
    )
    assert repeated.changed is False


def test_expired_report_requires_explicit_reservation_id() -> None:
    with pytest.raises(BrokerFactRecordingError, match="expired requires reservation_id"):
        record_broker_fact(
            load_portfolio_ledger(FIXTURE),
            decision_reference=PROPOSAL,
            status="expired",
            occurred_at=datetime(2026, 7, 31, 15, 30, tzinfo=JST),
            now=datetime(2026, 8, 1, 12, 0, tzinfo=JST),
        )


def test_expired_report_releases_only_remaining_partial_fill_quantity() -> None:
    expiry = datetime(2026, 7, 20, 15, 30, tzinfo=JST)
    now = datetime(2026, 7, 21, 12, 0, tzinfo=JST)
    opened = record_broker_fact(
        load_portfolio_ledger(FIXTURE),
        decision_reference=PROPOSAL,
        status="open",
        occurred_at=_at(9),
        ticker="1234",
        quantity=200,
        sector="サービス業",
        price_guard_yen=Decimal("900"),
        expires_at=expiry,
        now=now,
    )
    reservation = next(
        item
        for item in portfolio_snapshot(opened.document).active_reservations
        if item.ticker == "1234"
    )
    partial = record_broker_fact(
        opened.document,
        decision_reference=PROPOSAL,
        status="filled",
        occurred_at=_at(10),
        ticker="1234",
        quantity=100,
        price_yen=Decimal("890"),
        reservation_id=reservation.reservation_id,
        now=now,
    )
    partial_state = replay_events_through(partial.document.events, partial.document.as_of)
    assert (
        next(
            item
            for item in reservation_snapshots(partial_state)
            if item.reservation_id == reservation.reservation_id
        ).remaining_quantity
        == 100
    )
    expired = record_broker_fact(
        partial.document,
        decision_reference=PROPOSAL,
        status="expired",
        occurred_at=expiry,
        reservation_id=reservation.reservation_id,
        now=now,
    )
    state = replay_events_through(expired.document.events, expired.document.as_of)
    assert all(
        item.reservation_id != reservation.reservation_id for item in reservation_snapshots(state)
    )
    assert sum(lot.quantity for lot in state.lots["1234"]) == 100


def test_cancelled_report_keeps_human_status_even_after_recorded_expiry() -> None:
    result = record_broker_fact(
        load_portfolio_ledger(FIXTURE),
        decision_reference=PROPOSAL,
        status="cancelled",
        occurred_at=datetime(2026, 7, 31, 15, 30, tzinfo=JST),
        reservation_id="reservation-8929-pending",
        now=datetime(2026, 8, 1, 12, 0, tzinfo=JST),
    )
    assert result.document.events[-1].reason == "cancelled"


def test_filled_without_reservation_records_only_the_confirmed_execution() -> None:
    original = load_portfolio_ledger(FIXTURE)
    result = record_broker_fact(
        original,
        decision_reference=PROPOSAL,
        status="filled",
        occurred_at=_at(10),
        ticker="2331",
        quantity=100,
        price_yen=Decimal("1090"),
        now=_at(12),
    )
    assert len(result.event_ids) == 1
    times = [event.occurred_at for event in result.document.events]
    assert times == sorted(times)
    event = result.document.events[-1]
    assert event.type == "execution"
    assert event.reservation_id is None
    before = replay_events_through(original.events, original.as_of)
    after = replay_events_through(result.document.events, result.document.as_of)
    assert after.available_cash_yen == before.available_cash_yen - 109_000
    assert after.reserved_cash_yen == before.reserved_cash_yen
    assert sum(lot.quantity for lot in after.lots["2331"]) == 300


def test_result_rejects_an_empty_decision_reference() -> None:
    with pytest.raises(BrokerFactRecordingError, match="must be non-empty"):
        record_broker_fact(
            load_portfolio_ledger(FIXTURE),
            decision_reference="",
            status="cancelled",
            occurred_at=_at(10),
            reservation_id="reservation-8929-pending",
            now=_at(12),
        )


def test_result_rejects_future_human_times() -> None:
    ledger = load_portfolio_ledger(FIXTURE)
    with pytest.raises(BrokerFactRecordingError, match="occurred_at must not be in the future"):
        record_broker_fact(
            ledger,
            decision_reference=PROPOSAL,
            status="cancelled",
            occurred_at=_at(13),
            reservation_id="reservation-8929-pending",
            now=_at(12),
        )


def test_corrected_fill_is_a_conflict_not_a_silent_noop() -> None:
    opened = record_broker_fact(
        load_portfolio_ledger(FIXTURE),
        decision_reference=PROPOSAL,
        status="open",
        occurred_at=_at(9),
        ticker="2331",
        quantity=100,
        sector="サービス業",
        price_guard_yen=Decimal("1100"),
        expires_at=datetime(2026, 7, 20, 15, 30, tzinfo=JST),
        now=_at(12),
    )
    reservation_id = next(
        item.reservation_id
        for item in portfolio_snapshot(opened.document).active_reservations
        if item.ticker == "2331"
    )
    filled = record_broker_fact(
        opened.document,
        decision_reference=PROPOSAL,
        status="filled",
        occurred_at=_at(10),
        ticker="2331",
        quantity=100,
        price_yen=Decimal("1090"),
        reservation_id=reservation_id,
        now=_at(12),
    )
    with pytest.raises(BrokerFactRecordingError, match="conflicting human report"):
        record_broker_fact(
            filled.document,
            decision_reference=PROPOSAL,
            status="filled",
            occurred_at=_at(10),
            ticker="2331",
            quantity=100,
            price_yen=Decimal("1080"),
            reservation_id=reservation_id,
            now=_at(12),
        )


def test_fill_cannot_replace_the_reservation_decision_reference() -> None:
    opened = record_broker_fact(
        load_portfolio_ledger(FIXTURE),
        decision_reference=PROPOSAL,
        status="open",
        occurred_at=_at(9),
        ticker="2331",
        quantity=100,
        sector="サービス業",
        price_guard_yen=Decimal("1100"),
        expires_at=datetime(2026, 7, 20, 15, 30, tzinfo=JST),
        now=_at(12),
    )
    reservation_id = next(
        item.reservation_id
        for item in portfolio_snapshot(opened.document).active_reservations
        if item.ticker == "2331"
    )
    with pytest.raises(BrokerFactRecordingError, match="does not match the active reservation"):
        record_broker_fact(
            opened.document,
            decision_reference="https://github.com/koumatsumoto/baibai-loop/issues/361",
            status="filled",
            occurred_at=_at(10),
            ticker="2331",
            quantity=100,
            price_yen=Decimal("1090"),
            reservation_id=reservation_id,
            now=_at(12),
        )


@pytest.mark.parametrize("reserved", [False, True])
def test_equivalent_timezone_fill_retry_preserves_existing_event(reserved: bool) -> None:
    original = load_portfolio_ledger(FIXTURE)
    reservation_id = None
    if reserved:
        opened = record_broker_fact(
            original,
            decision_reference=PROPOSAL,
            status="open",
            occurred_at=_at(9),
            ticker="2331",
            quantity=100,
            price_guard_yen=Decimal("1100"),
            now=_at(12),
        )
        original = opened.document
        reservation_id = next(
            item.reservation_id
            for item in portfolio_snapshot(original).active_reservations
            if item.ticker == "2331"
        )
    first = record_broker_fact(
        original,
        decision_reference=PROPOSAL,
        status="filled",
        occurred_at=_at(10),
        ticker="2331",
        quantity=100,
        price_yen=Decimal("1090"),
        reservation_id=reservation_id,
        now=_at(12),
    )
    retry = record_broker_fact(
        first.document,
        decision_reference=PROPOSAL,
        status="filled",
        occurred_at=_at(10).astimezone(UTC),
        ticker="2331",
        quantity=100,
        price_yen=Decimal("1090"),
        reservation_id=reservation_id,
        now=_at(12),
    )
    assert not retry.changed
    assert retry.document == first.document
    assert (
        next(h.quantity for h in portfolio_snapshot(retry.document).holdings if h.ticker == "2331")
        == 300
    )


@pytest.mark.parametrize("explicit_ids", [False, True])
def test_equivalent_timezone_order_retry_keeps_one_reservation(explicit_ids: bool) -> None:
    original = load_portfolio_ledger(FIXTURE)
    reservation_id = "reported-reservation" if explicit_ids else None
    order_id = "reported-order" if explicit_ids else None
    first = record_broker_fact(
        original,
        decision_reference=PROPOSAL,
        status="open",
        occurred_at=_at(9),
        ticker="2331",
        quantity=100,
        price_guard_yen=Decimal("1100"),
        reservation_id=reservation_id,
        order_id=order_id,
        now=_at(12),
    )
    retry = record_broker_fact(
        first.document,
        decision_reference=PROPOSAL,
        status="open",
        occurred_at=_at(9).astimezone(UTC),
        ticker="2331",
        quantity=100,
        price_guard_yen=Decimal("1100"),
        reservation_id=reservation_id,
        order_id=order_id,
        now=_at(12),
    )
    assert not retry.changed
    assert retry.document == first.document


@pytest.mark.parametrize("status", ["open", "filled"])
@pytest.mark.parametrize("ticker", ["8929", "2331"])
def test_new_buy_does_not_inherit_a_closed_positions_classification(
    status: Literal["open", "filled"], ticker: str
) -> None:
    document = load_portfolio_ledger(FIXTURE)
    report = partial(record_broker_fact, decision_reference=PROPOSAL, ticker=ticker, now=_at(12))
    if ticker == "8929":  # Cancel the fixture's unfilled order.
        closed = report(
            document,
            status="cancelled",
            reservation_id="reservation-8929-pending",
            occurred_at=_at(10),
        ).document
    else:  # Sell the fixture's entire holding.
        sell = partial(report, side="sell", quantity=200, reservation_id="exit")
        order = sell(document, status="open", price_guard_yen=Decimal("1000"), occurred_at=_at(9))
        closed = sell(
            order.document, status="filled", price_yen=Decimal("1000"), occurred_at=_at(10)
        ).document

    buy = partial(
        report,
        closed,
        status=status,
        quantity=100,
        occurred_at=_at(11),
        price_guard_yen=Decimal("1100") if status == "open" else None,
        price_yen=Decimal("1100") if status == "filled" else None,
    )
    with pytest.raises(
        BrokerFactRecordingError, match="missing required human report fields: sector"
    ):
        buy()
    reopened = buy(sector="new-sector").document
    event = reopened.events[-1]
    assert isinstance(event, (ReservationEvent, ExecutionEvent))
    assert (event.sector, event.common_factors) == ("new-sector", ())
    assert reopened.events[:-1] == closed.events


@pytest.mark.parametrize("day", [2, 3, 5], ids=["open-order", "partial-fill", "remaining-holding"])
def test_live_exposure_keeps_its_classification(day: int) -> None:
    # Fixture 2331: reserved June 2, partially filled June 3, unfilled shares released June 5.
    result = record_broker_fact(
        load_portfolio_ledger(FIXTURE),
        decision_reference=PROPOSAL,
        status="filled",
        ticker="2331",
        quantity=100,
        price_yen=Decimal("1000"),
        now=_at(12),
        occurred_at=datetime(2026, 6, day, 17, tzinfo=JST),
    )
    event = next(event for event in result.document.events if event.event_id in result.event_ids)
    assert isinstance(event, ExecutionEvent)
    assert (event.sector, event.common_factors) == ("サービス業", ("labor-automation",))


def test_date_only_order_retry_uses_its_saved_classification_after_reentry() -> None:
    report = partial(record_broker_fact, ticker="1234", decision_reference=PROPOSAL, now=_at(12))
    order = partial(
        report,
        status="open",
        quantity=100,
        price_guard_yen=Decimal("1000"),
        reservation_id="original",
        occurred_on=_at(8).date(),
    )
    opened = order(
        load_portfolio_ledger(FIXTURE),
        occurred_at=_at(8),
        sector="old",
        common_factors=("old-factor",),
    )
    closed = report(
        opened.document, status="cancelled", reservation_id="original", occurred_at=_at(9)
    )
    new = report(
        closed.document,
        status="filled",
        quantity=100,
        price_yen=Decimal("1100"),
        sector="new",
        common_factors=("new-factor",),
        occurred_at=_at(10),
    )
    # Later facts on that date move the retry's internal replay key, not the order it denotes.
    assert not order(new.document, occurred_at=_at(10)).changed
    with pytest.raises(BrokerFactRecordingError, match="conflicting human report"):
        order(new.document, occurred_at=_at(10), sector="new")
