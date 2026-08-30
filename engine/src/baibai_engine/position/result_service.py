"""Build ledger drafts from human-reported broker facts and canonical decisions."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

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
from baibai_engine.research.assessment_service import BargainAssessmentService


def build_result_draft(
    ledger_service: LedgerStoreService,
    assessment_service: BargainAssessmentService,
    *,
    decision_reference: str,
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
    ordered_at: datetime | None = None,
    now: datetime | None = None,
) -> tuple[LedgerDraft | None, tuple[str, ...]]:
    """Build a draft from a canonical buy assessment or its active reservation.

    The assessment proves that the ticker passed research. Order terms are
    human-reported facts, not a persisted plan. Existing reservations keep the same
    decision reference for every partial or terminal result.
    """
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
        if any(item.decision_reference is None for item in selected):
            raise ValueError("active reservation has no canonical decision binding")
        if any(
            item.decision_reference is not None and item.decision_reference != decision_reference
            for item in selected
        ):
            raise ValueError("decision_reference does not match the active reservation")
    else:
        if status in {"cancelled", "expired"}:
            raise ValueError(f"{status} requires an active reservation")
        selected_case = assessment_service.require_buy_case(decision_reference)
        if ticker != selected_case.ticker:
            raise ValueError("broker result ticker does not match the buy assessment")

    if len(requested_ids) > 1:
        assert status in {"cancelled", "expired"}
        result = record_terminal_results(
            source,
            decision_reference=decision_reference,
            status=status,
            occurred_at=occurred_at,
            reservation_ids=requested_ids,
            now=now,
        )
    else:
        result = record_result(
            source,
            decision_reference=decision_reference,
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
            ordered_at=ordered_at,
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


__all__ = ["build_result_draft"]
