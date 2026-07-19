"""DB-bound broker-result drafts with proposal and reservation checks."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import cast

from baibai_engine.position.drafts import LedgerDraft
from baibai_engine.position.ledger import replay_events_through, reservation_snapshots
from baibai_engine.position.result_recording import ResultStatus, record_result
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.proposals.store import ProposalStoreService


def build_result_draft(
    ledger_service: LedgerStoreService,
    proposal_service: ProposalStoreService,
    *,
    proposal_id: str,
    status: ResultStatus,
    occurred_at: datetime,
    ticker: str | None = None,
    quantity: int | None = None,
    price_yen: Decimal | None = None,
    reservation_id: str | None = None,
    order_id: str | None = None,
    sector: str | None = None,
    common_factors: tuple[str, ...] = (),
    price_guard_yen: Decimal | None = None,
    expires_at: datetime | None = None,
    approved_at: datetime | None = None,
    now: datetime | None = None,
) -> tuple[LedgerDraft | None, tuple[str, ...]]:
    """Build a draft only from an approved proposal or its bound reservation."""
    source = ledger_service.load()
    reservations = reservation_snapshots(replay_events_through(source.events, source.as_of))
    reservation = next(
        (item for item in reservations if item.reservation_id == reservation_id),
        None,
    )
    if reservation is not None:
        if reservation.decision_reference != proposal_id:
            raise ValueError("proposal_id does not match the active reservation")
    else:
        proposal = proposal_service.get(proposal_id)
        if proposal.status != "approved":
            raise ValueError("new broker result requires an approved proposal")
        if status in {"cancelled", "expired"}:
            raise ValueError(f"{status} requires an active reservation")
        _match_approved_order(
            proposal.payload,
            ticker=ticker,
            quantity=quantity,
            price_guard_yen=price_guard_yen,
            expires_at=expires_at,
        )
    result = record_result(
        source,
        proposal_ref=proposal_id,
        status=status,
        occurred_at=occurred_at,
        ticker=ticker,
        quantity=quantity,
        price_yen=price_yen,
        reservation_id=reservation_id,
        order_id=order_id,
        sector=sector,
        common_factors=common_factors,
        price_guard_yen=price_guard_yen,
        expires_at=expires_at,
        approved_at=approved_at,
        now=now,
    )
    if not result.changed:
        return None, ()
    return (
        LedgerDraft(
            kind="record-result",
            expected_head=ledger_service.append_head(),
            source=source,
            replacement=result.document,
            confirmation_required=True,
        ),
        result.event_ids,
    )


def _match_approved_order(
    payload: object,
    *,
    ticker: str | None,
    quantity: int | None,
    price_guard_yen: Decimal | None,
    expires_at: datetime | None,
) -> None:
    if not isinstance(payload, dict):
        payload = dict(cast(dict[str, object], payload))
    generated = payload.get("execution_proposal")
    if not isinstance(generated, dict):
        raise ValueError("approved proposal payload is incomplete")
    if ticker != generated.get("ticker"):
        raise ValueError("broker result ticker does not match approved proposal")
    orders = generated.get("orders")
    if not isinstance(orders, list):
        raise ValueError("approved proposal has no executable orders")
    match = any(
        isinstance(order, dict)
        and order.get("quantity") == quantity
        and Decimal(str(order.get("limit_price_yen"))) == price_guard_yen
        and datetime.fromisoformat(str(order.get("expires_at"))) == expires_at
        for order in orders
    )
    if not match:
        raise ValueError("broker result order does not match approved proposal")


__all__ = ["build_result_draft"]
