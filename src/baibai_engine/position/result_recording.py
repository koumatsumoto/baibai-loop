"""Turn a human-reported broker result into a validated ledger draft."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal
from urllib.parse import urlparse

from baibai_engine.position.ledger import (
    ExecutionEvent,
    PortfolioLedgerDocument,
    PortfolioLedgerError,
    ReleaseEvent,
    ReservationSnapshot,
    replay_events_through,
    reservation_snapshots,
)

type ResultStatus = Literal["open", "filled", "cancelled", "expired"]


class ResultRecordingError(ValueError):
    """Raised when a human report lacks facts needed for a ledger event."""


@dataclass(frozen=True, slots=True)
class ResultRecordingResult:
    document: PortfolioLedgerDocument
    changed: bool
    event_ids: tuple[str, ...]


def record_result(
    document: PortfolioLedgerDocument,
    *,
    proposal_ref: str,
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
) -> ResultRecordingResult:
    """Append only facts explicitly supplied by the human operator.

    The function never reads a broker, guesses an execution, or mutates the
    input document. Deterministic repository identifiers may be generated, but
    broker facts are never inferred. The function returns a fully reconciled
    draft for separate review.
    """

    _validate_proposal_ref(proposal_ref)
    if occurred_at.tzinfo is None:
        raise ResultRecordingError("occurred_at must include a timezone")
    effective_now = now or datetime.now().astimezone()
    if effective_now.tzinfo is None:
        raise ResultRecordingError("now must include a timezone")
    if occurred_at > effective_now:
        raise ResultRecordingError("occurred_at must not be in the future")
    if approved_at is not None:
        if approved_at.tzinfo is None:
            raise ResultRecordingError("approved_at must include a timezone")
        if approved_at > effective_now:
            raise ResultRecordingError("approved_at must not be in the future")
    if status == "expired" and reservation_id is None:
        raise ResultRecordingError("expired requires reservation_id")

    existing_by_id = {event.event_id: event for event in document.events}
    if ticker is not None and status in {"open", "filled"}:
        suffix = _event_suffix(proposal_ref, status, ticker, occurred_at)
        prefix = "human-open" if status == "open" else "human-fill"
        existing = existing_by_id.get(f"{prefix}-{suffix}")
        if existing is not None and status == "filled":
            if not isinstance(existing, ExecutionEvent) or not _same_fill_report(
                existing,
                proposal_ref=proposal_ref,
                occurred_at=occurred_at,
                ticker=ticker,
                quantity=quantity,
                price_yen=price_yen,
                reservation_id=reservation_id,
            ):
                raise ResultRecordingError("conflicting human report for existing fill event")
            return ResultRecordingResult(document=document, changed=False, event_ids=())
    if status in {"cancelled", "expired"} and reservation_id is not None:
        release_reason: Literal["cancelled", "expired"] = (
            "cancelled" if status == "cancelled" else "expired"
        )
        releases = [
            event
            for event in document.events
            if isinstance(event, ReleaseEvent) and event.reservation_id == reservation_id
        ]
        if releases:
            if len(releases) != 1 or not _same_release_report(
                releases[0],
                reason=release_reason,
                proposal_ref=proposal_ref,
                occurred_at=occurred_at,
            ):
                raise ResultRecordingError("conflicting human report for released reservation")
            return ResultRecordingResult(document=document, changed=False, event_ids=())

    state = replay_events_through(document.events, document.as_of)
    holding_metadata = state.metadata.get(ticker) if ticker is not None else None
    resolved_sector = (
        sector if sector is not None else (holding_metadata[0] if holding_metadata else None)
    )
    resolved_common_factors = (
        common_factors if common_factors else (holding_metadata[1] if holding_metadata else ())
    )
    reservation = (
        None
        if status == "open"
        else _select_reservation(
            reservation_snapshots(state),
            reservation_id=reservation_id,
            ticker=ticker,
            required=status in {"cancelled", "expired"},
        )
    )
    additions: list[dict[str, object]] = []

    if status == "open":
        _require(ticker=ticker, quantity=quantity, sector=resolved_sector)
        assert ticker is not None
        if price_guard_yen is None or expires_at is None:
            raise ResultRecordingError("open requires price_guard_yen and expires_at")
        if expires_at <= occurred_at:
            raise ResultRecordingError("open expires_at must be after occurred_at")
        suffix = _event_suffix(proposal_ref, status, ticker, occurred_at)
        additions.append(
            {
                "event_id": f"human-open-{suffix}",
                "type": "reservation",
                "occurred_at": occurred_at.isoformat(),
                "reservation_id": reservation_id or f"reservation-{suffix}",
                "order_id": order_id or f"repository-order-{suffix}",
                "ticker": ticker,
                "sector": resolved_sector,
                "common_factors": sorted(resolved_common_factors),
                "decision_reference": proposal_ref,
                "quantity": quantity,
                "price_guard_yen": str(price_guard_yen),
                "expires_at": expires_at.isoformat(),
            }
        )
    elif status == "filled":
        _require(ticker=ticker, quantity=quantity)
        assert ticker is not None
        if price_yen is None:
            raise ResultRecordingError("filled requires price_yen")
        if reservation is None:
            if (
                approved_at is None
                or price_guard_yen is None
                or expires_at is None
                or resolved_sector is None
            ):
                raise ResultRecordingError(
                    "filled without an active reservation requires approved_at, "
                    "price_guard_yen, expires_at, and sector"
                )
            if not approved_at < occurred_at < expires_at:
                raise ResultRecordingError("filled requires approved_at < occurred_at < expires_at")
            suffix = _event_suffix(proposal_ref, "open", ticker, approved_at)
            resolved_reservation_id = reservation_id or f"reservation-{suffix}"
            additions.append(
                {
                    "event_id": f"human-open-{suffix}",
                    "type": "reservation",
                    "occurred_at": approved_at.isoformat(),
                    "reservation_id": resolved_reservation_id,
                    "order_id": order_id or f"repository-order-{suffix}",
                    "ticker": ticker,
                    "sector": resolved_sector,
                    "common_factors": sorted(resolved_common_factors),
                    "decision_reference": proposal_ref,
                    "quantity": quantity,
                    "price_guard_yen": str(price_guard_yen),
                    "expires_at": expires_at.isoformat(),
                }
            )
        else:
            if ticker != reservation.ticker:
                raise ResultRecordingError("filled ticker does not match reservation")
            if (
                reservation.decision_reference is not None
                and proposal_ref != reservation.decision_reference
            ):
                raise ResultRecordingError(
                    "filled proposal_ref does not match the active reservation"
                )
            resolved_reservation_id = reservation.reservation_id
        suffix = _event_suffix(proposal_ref, status, ticker, occurred_at)
        additions.append(
            {
                "event_id": f"human-fill-{suffix}",
                "type": "execution",
                "occurred_at": occurred_at.isoformat(),
                "execution_id": f"execution-{suffix}",
                "reservation_id": resolved_reservation_id,
                "ticker": ticker,
                "side": "buy",
                "quantity": quantity,
                "price_yen": str(price_yen),
                "decision_reference": proposal_ref,
            }
        )
    else:
        assert reservation is not None
        if (
            reservation.decision_reference is not None
            and proposal_ref != reservation.decision_reference
        ):
            raise ResultRecordingError(
                f"{status} proposal_ref does not match the active reservation"
            )
        if status == "expired" and occurred_at < reservation.expires_at:
            raise ResultRecordingError("expired occurred_at must be at or after expires_at")
        suffix = _event_suffix(proposal_ref, status, reservation.ticker, occurred_at)
        prefix = "human-cancel" if status == "cancelled" else "human-expire"
        additions.append(
            {
                "event_id": f"{prefix}-{suffix}",
                "type": "release",
                "occurred_at": occurred_at.isoformat(),
                "reservation_id": reservation.reservation_id,
                "reason": status,
                "decision_reference": proposal_ref,
            }
        )

    pending: list[dict[str, object]] = []
    for addition in additions:
        event_id = str(addition["event_id"])
        existing = existing_by_id.get(event_id)
        if existing is None:
            pending.append(addition)
            continue
        if existing.model_dump(mode="json") != addition:
            raise ResultRecordingError(f"conflicting human report for event {event_id}")
    if not pending:
        return ResultRecordingResult(document=document, changed=False, event_ids=())
    raw = document.model_dump(mode="json")
    raw["events"] = sorted(
        [*raw["events"], *pending],
        key=lambda event: datetime.fromisoformat(str(event["occurred_at"])),
    )
    raw["as_of"] = max(document.as_of, occurred_at).isoformat()
    try:
        patched = PortfolioLedgerDocument.model_validate(raw)
        replay_events_through(patched.events, patched.as_of)
    except (PortfolioLedgerError, ValueError) as error:
        raise ResultRecordingError(f"reported result does not reconcile: {error}") from error
    return ResultRecordingResult(
        document=patched,
        changed=True,
        event_ids=tuple(str(item["event_id"]) for item in pending),
    )


def _select_reservation(
    reservations: tuple[ReservationSnapshot, ...],
    *,
    reservation_id: str | None,
    ticker: str | None,
    required: bool,
) -> ReservationSnapshot | None:
    matches = [
        item
        for item in reservations
        if (reservation_id is None or item.reservation_id == reservation_id)
        and (ticker is None or item.ticker == ticker)
    ]
    if len(matches) > 1:
        raise ResultRecordingError("multiple active reservations match; provide reservation_id")
    if not matches:
        if required:
            raise ResultRecordingError("no active reservation matches the human report")
        return None
    return matches[0]


def _require(**values: object) -> None:
    missing = [name for name, value in values.items() if value is None]
    if missing:
        raise ResultRecordingError("missing required human report fields: " + ", ".join(missing))


def _event_suffix(proposal_ref: str, status: str, ticker: str, occurred_at: datetime) -> str:
    raw = f"{proposal_ref}|{status}|{ticker}|{occurred_at.isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _validate_proposal_ref(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.netloc != "github.com" or "/issues/" not in parsed.path:
        raise ResultRecordingError("proposal_ref must be an HTTPS GitHub Issue URL")


def _same_fill_report(
    event: ExecutionEvent,
    *,
    proposal_ref: str,
    occurred_at: datetime,
    ticker: str,
    quantity: int | None,
    price_yen: Decimal | None,
    reservation_id: str | None,
) -> bool:
    return (
        event.decision_reference == proposal_ref
        and event.occurred_at == occurred_at
        and event.ticker == ticker
        and event.quantity == quantity
        and event.price_yen == price_yen
        and (reservation_id is None or event.reservation_id == reservation_id)
    )


def _same_release_report(
    event: ReleaseEvent,
    *,
    reason: Literal["cancelled", "expired"],
    proposal_ref: str,
    occurred_at: datetime,
) -> bool:
    return (
        event.reason == reason
        and event.decision_reference == proposal_ref
        and event.occurred_at == occurred_at
    )
