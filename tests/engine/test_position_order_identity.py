"""保存注文の明示IDでcash・残数・terminal再試行を束縛する合成回帰。"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from tests.helpers.db_seed import seed_ledger

from baibai_engine.position.drafts import apply_draft, build_broker_fact_draft
from baibai_engine.position.ledger import (
    ExecutionEvent,
    PortfolioLedgerDocument,
    replay_events_through,
)
from baibai_engine.position.store import LedgerStoreService

AT = datetime.fromisoformat("2026-10-20T10:00:00+09:00")
REFERENCE = "synthetic-buy-reference"


def _service(tmp_path: Path, cash: int = 1_000_000) -> LedgerStoreService:
    path = tmp_path / "app.sqlite"
    seed_ledger(
        path,
        PortfolioLedgerDocument.model_validate(
            {
                "schema_version": 3,
                "portfolio_scope": "repository_only",
                "as_of": AT,
                "events": [
                    {
                        "type": "opening_balance",
                        "event_id": "synthetic-opening",
                        "occurred_at": AT,
                        "amount_yen": cash,
                    }
                ],
            }
        ),
    )
    return LedgerStoreService(path)


def _open(
    service: LedgerStoreService,
    order: str,
    quantity: int = 100,
    *,
    direct_fact: bool = False,
    side: str = "buy",
) -> str:
    draft, _ = build_broker_fact_draft(
        service,
        lambda _: "1234",
        decision_reference=None if direct_fact else REFERENCE,
        direct_fact=direct_fact,
        status="open",
        side=side,
        occurred_on=AT.date(),
        ticker="1234",
        quantity=quantity,
        sector="合成業種",
        order_id=order,
        price_guard_yen=Decimal(1000),
        expires_at=AT + timedelta(days=1),
        now=AT,
    )
    assert draft is not None
    apply_draft(service, draft, human_confirmed=True)
    return draft.replacement.events[-1].reservation_id


@pytest.mark.parametrize("cash", [200_000, 1_000_000])
@pytest.mark.parametrize("direct_fact", [False, True])
@pytest.mark.parametrize("explicit_reservation", [False, True])
def test_order_id_consumes_only_its_reserved_quantity(
    tmp_path: Path, cash: int, direct_fact: bool, explicit_reservation: bool
):
    service = _service(tmp_path, cash)
    reservation_id = _open(service, "synthetic-order", quantity=200, direct_fact=direct_fact)

    def unavailable_reference(_reference: str) -> str:
        pytest.fail("the saved order must supply its existing decision binding")

    report = {
        "decision_reference": None if direct_fact else REFERENCE,
        "direct_fact": direct_fact,
        "status": "filled",
        "occurred_on": AT.date(),
        "ticker": "1234",
        "quantity": 100,
        "price_yen": Decimal(900),
        "sector": "合成業種",
        "order_id": "synthetic-order",
        "now": AT,
    }
    if explicit_reservation:
        report["reservation_id"] = reservation_id
    for index in (1, 2):
        draft, _ = build_broker_fact_draft(
            service, unavailable_reference, **report, execution_id=f"synthetic-fill-{index}"
        )
        assert draft is not None
        fill = draft.replacement.events[-1]
        assert isinstance(fill, ExecutionEvent)
        assert fill.reservation_id == reservation_id
        assert draft.binding_addition is None
        apply_draft(service, draft, human_confirmed=True)
        document = service.load()
        state = replay_events_through(document.events, document.as_of)
        assert state.reserved_cash_yen == (200 - 100 * index) * 1000
        assert state.available_cash_yen == cash - 200_000 + 10_000 * index
        assert sum(lot.quantity for lot in state.lots["1234"]) == 100 * index
        assert build_broker_fact_draft(
            service, unavailable_reference, **report, execution_id=f"synthetic-fill-{index}"
        ) == (None, ())
    assert not state.active_reservations
    with pytest.raises(ValueError, match="order_id conflicts"):
        build_broker_fact_draft(
            service,
            unavailable_reference,
            **(report | {"order_id": "wrong-order"}),
            execution_id="synthetic-fill-2",
        )


def test_proposal_fill_rejects_conflicting_order_and_reservation_ids(tmp_path: Path):
    service = _service(tmp_path)
    first = _open(service, "synthetic-order-1")
    second = _open(service, "synthetic-order-2")
    before = service.load_with_head()
    with pytest.raises(ValueError, match="different orders"):
        build_broker_fact_draft(
            service,
            lambda _: "1234",
            decision_reference=REFERENCE,
            status="filled",
            occurred_on=AT.date(),
            ticker="1234",
            quantity=100,
            price_yen=Decimal(900),
            reservation_id=first,
            order_id="synthetic-order-2",
            execution_id="synthetic-fill",
            now=AT,
        )
    assert service.load_with_head() == before

    def unavailable_reference(_reference: str) -> str:
        pytest.fail("a unique saved order must supply its existing decision binding")

    draft, _ = build_broker_fact_draft(
        service,
        unavailable_reference,
        decision_reference=REFERENCE,
        status="filled",
        occurred_on=AT.date(),
        ticker="1234",
        quantity=100,
        price_yen=Decimal(900),
        order_id="synthetic-order-2",
        execution_id="synthetic-fill",
        now=AT,
    )
    assert draft is not None
    fill = draft.replacement.events[-1]
    assert isinstance(fill, ExecutionEvent)
    assert fill.reservation_id == second
    apply_draft(service, draft, human_confirmed=True)
    document = service.load()
    state = replay_events_through(document.events, document.as_of)
    assert set(state.active_reservations) == {first}
    assert state.active_reservations[first].remaining_quantity == 100
    assert state.reserved_cash_yen == 100_000
    assert state.available_cash_yen == 810_000
    assert sum(lot.quantity for lot in state.lots["1234"]) == 100


@pytest.mark.parametrize(
    ("reported_order", "reported_reservation", "error"),
    [
        ("unknown-order", "saved", "order_id conflicts"),
        ("synthetic-order", "unknown-reservation", "requires a saved reservation"),
    ],
)
def test_unknown_identity_cannot_override_an_explicit_saved_identity(
    tmp_path: Path, reported_order: str, reported_reservation: str, error: str
):
    service = _service(tmp_path)
    saved = _open(service, "synthetic-order")
    before = service.load_with_head()
    with pytest.raises(ValueError, match=error):
        build_broker_fact_draft(
            service,
            lambda _: "1234",
            decision_reference=REFERENCE,
            status="filled",
            occurred_on=AT.date(),
            ticker="1234",
            quantity=100,
            price_yen=Decimal(900),
            sector="合成業種",
            order_id=reported_order,
            reservation_id=saved if reported_reservation == "saved" else reported_reservation,
            execution_id="synthetic-fill",
            now=AT,
        )
    assert service.load_with_head() == before


def test_unknown_order_without_reservation_records_only_the_unregistered_fact(tmp_path: Path):
    service = _service(tmp_path)
    reservation_id = _open(service, "unrelated-order")
    report = {
        "decision_reference": REFERENCE,
        "status": "filled",
        "occurred_on": AT.date(),
        "ticker": "1234",
        "quantity": 100,
        "price_yen": Decimal(900),
        "sector": "合成業種",
        "order_id": "unregistered-order",
        "execution_id": "unregistered-fill",
        "now": AT,
    }
    draft, _ = build_broker_fact_draft(service, lambda _: "1234", **report)
    assert draft is not None
    assert draft.replacement.events[-1].reservation_id is None
    apply_draft(service, draft, human_confirmed=True)
    document = service.load()
    state = replay_events_through(document.events, document.as_of)
    assert state.available_cash_yen == 810_000
    assert state.reserved_cash_yen == 100_000
    assert state.active_reservations[reservation_id].remaining_quantity == 100
    assert build_broker_fact_draft(service, lambda _: "1234", **report) == (None, ())


@pytest.mark.parametrize(
    ("quantity", "price", "error"),
    [(300, "900", "exceeds reserved quantity"), (100, "1001", "exceeds reservation price guard")],
)
def test_proposal_order_id_cannot_bypass_remaining_or_guard(
    tmp_path: Path, quantity: int, price: str, error: str
):
    service = _service(tmp_path)
    _open(service, "synthetic-order", quantity=200)
    before = service.load_with_head()
    with pytest.raises(ValueError, match=error):
        build_broker_fact_draft(
            service,
            lambda _: "1234",
            decision_reference=REFERENCE,
            status="filled",
            occurred_on=AT.date(),
            ticker="1234",
            quantity=quantity,
            price_yen=Decimal(price),
            sector="合成業種",
            order_id="synthetic-order",
            execution_id="synthetic-fill",
            now=AT,
        )
    assert service.load_with_head() == before


@pytest.mark.parametrize("status", ["cancelled", "expired"])
@pytest.mark.parametrize("date_only", [True, False])
def test_distinct_orders_can_report_terminal_facts_at_the_same_occurrence(
    tmp_path: Path, status: str, date_only: bool
):
    service = _service(tmp_path)
    reservation_ids = [_open(service, f"synthetic-order-{index}") for index in (1, 2)]
    terminal_at = AT + timedelta(days=1, hours=1)
    occurrence: dict[str, date | datetime] = (
        {"occurred_on": terminal_at.date()} if date_only else {"occurred_at": terminal_at}
    )
    for reservation_id in reservation_ids:
        report = {
            "decision_reference": REFERENCE,
            "status": status,
            "reservation_id": reservation_id,
            "now": terminal_at,
        }
        draft, _ = build_broker_fact_draft(service, lambda _: "1234", **report, **occurrence)
        assert draft is not None
        apply_draft(service, draft, human_confirmed=True)
        assert build_broker_fact_draft(service, lambda _: "1234", **report, **occurrence) == (
            None,
            (),
        )
    document = service.load()
    state = replay_events_through(document.events, document.as_of)
    assert state.reserved_cash_yen == 0
    assert state.available_cash_yen == 1_000_000
    assert not state.active_reservations


@pytest.mark.parametrize("status", ["cancelled", "expired"])
def test_order_id_terminal_and_prior_partial_fill_retries_after_release(
    tmp_path: Path, status: str
):
    service = _service(tmp_path)
    _open(service, "synthetic-order", quantity=200)

    def unavailable_reference(_reference: str) -> str:
        pytest.fail("known order reports must not resolve a current buy binding")

    report = {
        "decision_reference": REFERENCE,
        "status": "filled",
        "occurred_on": AT.date(),
        "ticker": "1234",
        "quantity": 100,
        "price_yen": Decimal(900),
        "order_id": "synthetic-order",
        "execution_id": "synthetic-fill",
        "now": AT,
    }
    fill, _ = build_broker_fact_draft(service, unavailable_reference, **report)
    assert fill is not None
    apply_draft(service, fill, human_confirmed=True)
    old_events = service.load().events
    terminal_at = AT + timedelta(days=1, hours=1)
    terminal_report = {
        "decision_reference": REFERENCE,
        "status": status,
        "occurred_on": terminal_at.date(),
        "order_id": "synthetic-order",
        "now": terminal_at,
    }
    terminal, _ = build_broker_fact_draft(service, unavailable_reference, **terminal_report)
    assert terminal is not None
    apply_draft(service, terminal, human_confirmed=True)
    document, head = service.load_with_head()
    assert document.events[: len(old_events)] == old_events
    state = replay_events_through(document.events, document.as_of)
    assert not state.active_reservations
    assert state.available_cash_yen == 910_000
    assert build_broker_fact_draft(service, unavailable_reference, **terminal_report) == (None, ())
    assert build_broker_fact_draft(service, unavailable_reference, **report) == (None, ())
    with pytest.raises(ValueError, match="conflicting human report"):
        build_broker_fact_draft(
            service, unavailable_reference, **(report | {"price_yen": Decimal(901)})
        )
    with pytest.raises(ValueError, match="no active reservation"):
        build_broker_fact_draft(
            service,
            lambda _: "1234",
            **(report | {"execution_id": "new-fill-after-release"}),
        )
    with pytest.raises(ValueError, match="decision_reference conflicts"):
        build_broker_fact_draft(
            service,
            unavailable_reference,
            **(terminal_report | {"decision_reference": "different-reference"}),
        )
    assert service.load_with_head() == (document, head)


def test_sell_order_id_consumes_shares_and_retries_after_full_fill(tmp_path: Path):
    service = _service(tmp_path)
    bought, _ = build_broker_fact_draft(
        service,
        lambda _: "1234",
        decision_reference=REFERENCE,
        status="filled",
        occurred_on=AT.date(),
        ticker="1234",
        quantity=200,
        price_yen=Decimal(900),
        sector="合成業種",
        execution_id="synthetic-buy",
        now=AT,
    )
    assert bought is not None
    apply_draft(service, bought, human_confirmed=True)
    reservation_id = _open(service, "synthetic-sell-order", quantity=200, side="sell")

    def unavailable_reference(_reference: str) -> str:
        pytest.fail("sell facts must not resolve a buy binding")

    report = {
        "decision_reference": REFERENCE,
        "status": "filled",
        "side": "sell",
        "occurred_on": AT.date(),
        "ticker": "1234",
        "quantity": 100,
        "price_yen": Decimal(1100),
        "order_id": "synthetic-sell-order",
        "now": AT,
    }
    for index in (1, 2):
        fill, _ = build_broker_fact_draft(
            service, unavailable_reference, **report, execution_id=f"synthetic-sell-{index}"
        )
        assert fill is not None
        assert fill.replacement.events[-1].reservation_id == reservation_id
        apply_draft(service, fill, human_confirmed=True)
        document = service.load()
        state = replay_events_through(document.events, document.as_of)
        assert state.available_cash_yen == 820_000 + 110_000 * index
        assert state.reserved_cash_yen == 0
        assert sum(lot.quantity for lot in state.lots["1234"]) == 200 - 100 * index
        assert state.realized_gross_pnl_yen == 20_000 * index
        if index == 1:
            assert state.active_reservations[reservation_id].remaining_quantity == 100
        else:
            assert not state.active_reservations
        assert build_broker_fact_draft(
            service, unavailable_reference, **report, execution_id=f"synthetic-sell-{index}"
        ) == (None, ())
