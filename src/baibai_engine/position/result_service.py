"""DB-bound broker-result drafts with proposal and reservation checks."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import cast
from urllib.parse import urlparse

from baibai_engine.position.drafts import LedgerDraft
from baibai_engine.position.ledger import (
    ReleaseEvent,
    replay_events_through,
    reservation_snapshots,
)
from baibai_engine.position.result_recording import (
    ResultStatus,
    record_result,
    record_terminal_results,
)
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
    reservation_ids: tuple[str, ...] = (),
    order_id: str | None = None,
    sector: str | None = None,
    common_factors: tuple[str, ...] = (),
    price_guard_yen: Decimal | None = None,
    expires_at: datetime | None = None,
    approved_at: datetime | None = None,
    now: datetime | None = None,
) -> tuple[LedgerDraft | None, tuple[str, ...]]:
    """Build a draft only from an approved proposal or its bound reservation."""
    if reservation_id is not None and reservation_ids:
        raise ValueError("provide reservation_id or reservation_ids, not both")
    requested_ids = reservation_ids or (() if reservation_id is None else (reservation_id,))
    if len(requested_ids) > 1 and status not in {"cancelled", "expired"}:
        raise ValueError("multiple reservation_ids require a terminal result")
    source = ledger_service.load()
    reservations = reservation_snapshots(replay_events_through(source.events, source.as_of))
    reservations_by_id = {item.reservation_id: item for item in reservations}
    released_ids = {
        event.reservation_id for event in source.events if isinstance(event, ReleaseEvent)
    }
    selected = tuple(
        reservations_by_id[item] for item in requested_ids if item in reservations_by_id
    )
    known_terminal = (
        status in {"cancelled", "expired"}
        and bool(requested_ids)
        and all(item in reservations_by_id or item in released_ids for item in requested_ids)
    )
    if selected or known_terminal:
        if status in {"cancelled", "expired"} and any(
            item not in reservations_by_id and item not in released_ids for item in requested_ids
        ):
            raise ValueError(f"{status} requires an active reservation")
        # Migration reservations predate proposal persistence and carry no
        # decision reference. Their terminal human report is bound to the
        # supplied issue URL by the release event itself. Native reservations
        # keep their proposal binding and cannot be reassigned here.
        migration_reservations = tuple(item for item in selected if item.decision_reference is None)
        if migration_reservations:
            if status not in {"cancelled", "expired"}:
                raise ValueError(
                    "migration reservation without proposal binding only supports terminal result"
                )
            _require_migration_issue_reference(proposal_id)
        if any(
            item.decision_reference is not None and item.decision_reference != proposal_id
            for item in selected
        ):
            raise ValueError("proposal_id does not match the active reservation")
    else:
        if status in {"cancelled", "expired"}:
            raise ValueError(f"{status} requires an active reservation")
        proposal = proposal_service.get(proposal_id)
        if proposal.status != "approved":
            raise ValueError("new broker result requires an approved proposal")
        _match_approved_order(
            proposal.payload,
            ticker=ticker,
            quantity=quantity,
            price_guard_yen=price_guard_yen,
            expires_at=expires_at,
        )
    if len(requested_ids) > 1:
        assert status in {"cancelled", "expired"}
        result = record_terminal_results(
            source,
            proposal_ref=proposal_id,
            status=status,
            occurred_at=occurred_at,
            reservation_ids=requested_ids,
            now=now,
        )
    else:
        result = record_result(
            source,
            proposal_ref=proposal_id,
            status=status,
            occurred_at=occurred_at,
            ticker=ticker,
            quantity=quantity,
            price_yen=price_yen,
            reservation_id=requested_ids[0] if requested_ids else None,
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


def _require_migration_issue_reference(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.netloc != "github.com" or "/issues/" not in parsed.path:
        raise ValueError("migration terminal result requires an HTTPS GitHub Issue URL")


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
