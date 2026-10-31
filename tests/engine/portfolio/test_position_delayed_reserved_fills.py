"""保存済みreleaseより前の遅延部分約定を、既存eventを変えずに追記する。"""

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
from tests.helpers.db_seed import seed_ledger

from baibai_engine.portfolio.ledger.drafts import (
    LedgerDraft,
    apply_draft,
    build_broker_fact_draft,
    build_sell_execution_draft,
)
from baibai_engine.portfolio.ledger.events import (
    ExecutionEvent,
    PortfolioLedgerDocument,
    replay_events_through,
)
from baibai_engine.portfolio.ledger.store import LedgerStoreService

AT = datetime.fromisoformat("2026-10-20T10:00:00+09:00")
ORDER_AT = AT + timedelta(days=1)
KNOWN_FILL_AT = AT + timedelta(days=2)
LATE_FILL_AT = AT + timedelta(days=3)
RELEASE_AT = AT + timedelta(days=4)
REFERENCE = "synthetic-saved-reference"
Kind = Literal["buy", "sell", "sell-execution"]
Identity = Literal["order", "reservation"]
CASES = [
    ("buy", "order"),
    ("buy", "reservation"),
    ("sell", "order"),
    ("sell", "reservation"),
    ("sell-execution", "reservation"),
]


def _released_order(
    tmp_path: Path, kind: Kind, prior_quantity: int, *, reason: str = "cancelled"
) -> LedgerStoreService:
    side = "buy" if kind == "buy" else "sell"
    events: list[dict[str, object]] = [
        {"type": "opening_balance", "event_id": "opening", "occurred_at": AT, "amount_yen": 1000000}
    ]
    if side == "sell":
        events.append(
            {
                "type": "execution",
                "event_id": "initial-buy",
                "execution_id": "initial-buy",
                "occurred_at": AT,
                "side": "buy",
                "ticker": "1234",
                "quantity": 400,
                "price_yen": "900",
                "sector": "合成業種",
            }
        )
    events.append(
        {
            "type": "reservation",
            "event_id": "saved-order",
            "reservation_id": "saved-reservation",
            "order_id": "saved-order",
            "occurred_at": ORDER_AT,
            "side": side,
            "ticker": "1234",
            "quantity": prior_quantity + 200,
            "price_guard_yen": "1000",
            "expires_at": RELEASE_AT,
            "sector": "合成業種",
            "decision_reference": REFERENCE,
        }
    )
    if prior_quantity:
        events.append(
            {
                "type": "execution",
                "event_id": "known-fill",
                "execution_id": "known-fill",
                "reservation_id": "saved-reservation",
                "occurred_at": KNOWN_FILL_AT,
                "side": side,
                "ticker": "1234",
                "quantity": prior_quantity,
                "price_yen": "900" if side == "buy" else "1100",
                "decision_reference": REFERENCE,
            }
        )
    events.append(
        {
            "type": "release",
            "event_id": "saved-release",
            "reservation_id": "saved-reservation",
            "occurred_at": RELEASE_AT,
            "reason": reason,
            "decision_reference": REFERENCE,
        }
    )
    database = tmp_path / "app.sqlite"
    seed_ledger(
        database,
        PortfolioLedgerDocument.model_validate(
            {
                "schema_version": 3,
                "portfolio_scope": "repository_only",
                "as_of": RELEASE_AT,
                "events": events,
            }
        ),
    )
    return LedgerStoreService(database)


def _fill(
    service: LedgerStoreService,
    kind: Kind,
    identity: Identity,
    *,
    occurred_at: datetime = LATE_FILL_AT,
    date_only: bool = False,
    quantity: int = 100,
    price_yen: Decimal | None = None,
) -> LedgerDraft | None:
    occurrence = {"occurred_on": occurred_at.date()} if date_only else {"occurred_at": occurred_at}
    price = price_yen if price_yen is not None else Decimal(900 if kind == "buy" else 1100)
    if kind == "sell-execution":
        return build_sell_execution_draft(
            service,
            **occurrence,
            ticker="1234",
            quantity=quantity,
            price_yen=price,
            reservation_id="saved-reservation",
            execution_id="late-fill",
            fees_yen=100,
            tax_yen=200,
        )

    def unavailable_reference(_reference: str) -> str:
        pytest.fail("the saved order must supply its existing decision binding")

    draft, _ = build_broker_fact_draft(
        service,
        unavailable_reference,
        **occurrence,
        decision_reference=REFERENCE,
        status="filled",
        side=kind,
        ticker="1234",
        quantity=quantity,
        price_yen=price,
        reservation_id="saved-reservation" if identity == "reservation" else None,
        order_id="saved-order" if identity == "order" else None,
        execution_id="late-fill",
        now=RELEASE_AT + timedelta(days=1),
    )
    return draft


@pytest.mark.parametrize(("kind", "identity"), CASES)
@pytest.mark.parametrize("prior_quantity", [0, 100])
@pytest.mark.parametrize("date_only", [False, True])
@pytest.mark.parametrize("reason", ["cancelled", "expired"])
def test_delayed_partial_fill_precedes_saved_release_without_rewriting_events(
    tmp_path: Path,
    kind: Kind,
    identity: Identity,
    prior_quantity: int,
    date_only: bool,
    reason: str,
) -> None:
    service = _released_order(tmp_path, kind, prior_quantity, reason=reason)
    before, head = service.load_with_head()

    draft = _fill(service, kind, identity, date_only=date_only)

    assert draft is not None
    assert service.load_with_head() == (before, head)
    previous_ids = {event.event_id for event in before.events}
    retained_events = tuple(
        event for event in draft.replacement.events if event.event_id in previous_ids
    )
    assert retained_events == before.events
    fill = next(
        event
        for event in draft.replacement.events
        if isinstance(event, ExecutionEvent) and event.execution_id == "late-fill"
    )
    assert fill.reservation_id == "saved-reservation"
    assert fill.decision_reference == REFERENCE
    assert fill.occurred_on == (LATE_FILL_AT.date() if date_only else None)
    before_release = replay_events_through(draft.replacement.events, LATE_FILL_AT)
    assert before_release.active_reservations["saved-reservation"].remaining_quantity == 100
    assert before_release.reserved_cash_yen == (100000 if kind == "buy" else 0)
    apply_draft(service, draft, human_confirmed=True)

    saved = service.load()
    state = replay_events_through(saved.events, saved.as_of)
    assert saved.as_of == RELEASE_AT
    assert not state.active_reservations
    assert state.reserved_cash_yen == 0
    filled_quantity = prior_quantity + 100
    if kind == "buy":
        assert state.available_cash_yen == 1000000 - filled_quantity * 900
        assert sum(lot.quantity for lot in state.lots["1234"]) == filled_quantity
    else:
        assert state.available_cash_yen == (
            640000 + filled_quantity * 1100 - (300 if kind == "sell-execution" else 0)
        )
        assert sum(lot.quantity for lot in state.lots["1234"]) == 400 - filled_quantity
        assert state.realized_gross_pnl_yen == filled_quantity * 200
    assert _fill(service, kind, identity, date_only=date_only) is None
    after = service.load_with_head()
    with pytest.raises(ValueError, match="conflicting human report"):
        _fill(service, kind, identity, date_only=date_only, price_yen=fill.price_yen + 1)
    assert service.load_with_head() == after


@pytest.mark.parametrize(("kind", "identity"), CASES)
@pytest.mark.parametrize("case", ["before-order", "after-release", "oversized", "consumes-release"])
def test_delayed_fill_must_reconcile_at_insertion_and_through_the_saved_release(
    tmp_path: Path, kind: Kind, identity: Identity, case: str
) -> None:
    service = _released_order(tmp_path, kind, prior_quantity=100)
    before = service.load_with_head()
    occurred_at = {
        "before-order": ORDER_AT - timedelta(minutes=1),
        "after-release": RELEASE_AT,
    }.get(case, LATE_FILL_AT)
    quantity = {"oversized": 400, "consumes-release": 200}.get(case, 100)

    error = (
        "release references no active reservation"
        if case == "consumes-release"
        else ("exceeds" if case == "oversized" else "active")
    )
    with pytest.raises(ValueError, match=error):
        _fill(service, kind, identity, occurred_at=occurred_at, quantity=quantity)

    assert service.load_with_head() == before


@pytest.mark.parametrize(("kind", "identity"), CASES)
def test_date_only_fill_cannot_be_moved_before_a_saved_same_day_release(
    tmp_path: Path, kind: Kind, identity: Identity
) -> None:
    service = _released_order(tmp_path, kind, prior_quantity=0)
    before = service.load_with_head()
    with pytest.raises(ValueError, match="active"):
        _fill(service, kind, identity, occurred_at=RELEASE_AT, date_only=True)
    assert service.load_with_head() == before
