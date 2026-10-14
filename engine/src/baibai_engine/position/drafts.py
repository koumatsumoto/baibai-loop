"""Ephemeral, source-bound drafts for explicit human-confirmed ledger writes."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from baibai_engine.foundation.time import JST
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.position.broker_fact_recording import (
    BrokerFactRecordingResult,
    BrokerFactStatus,
    _provided_execution_suffix,
    _validate_execution_id,
    record_broker_fact,
    record_terminal_broker_facts,
)
from baibai_engine.position.decision_references import DecisionReferenceBinding
from baibai_engine.position.ledger import (
    ConfirmedTaxEvent,
    ContributionEvent,
    CostEvent,
    ExecutionEvent,
    HumanOverride,
    IncomeEvent,
    PortfolioLedgerDocument,
    ReleaseEvent,
    ReservationEvent,
    WithdrawalEvent,
    active_human_overrides,
    replay_events_through,
    require_resolved_expiries,
    reservation_snapshots,
)
from baibai_engine.position.store import LedgerApplyResult, LedgerStoreService

DraftKind = Literal["event", "broker-fact", "sell-execution", "override", "meta"]
HumanEvent = ContributionEvent | WithdrawalEvent | IncomeEvent | CostEvent | ConfirmedTaxEvent


class LedgerDraft(BaseModel):
    """A reviewable replacement bound to the exact DB state it was built from."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    schema_version: Literal[2] = 2
    kind: DraftKind
    expected_head: int = Field(ge=0)
    source: PortfolioLedgerDocument
    replacement: PortfolioLedgerDocument
    confirmation_required: bool = True
    binding_addition: DecisionReferenceBinding | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def _version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("ledger draft schema_version requires an integer; prepare a new draft")
        return value

    @model_validator(mode="after")
    def _binding_kind(self) -> LedgerDraft:
        if self.binding_addition is not None and self.kind != "broker-fact":
            raise ValueError("only a direct buy broker-fact may add a decision binding")
        return self


def build_broker_fact_draft(
    ledger_service: LedgerStoreService,
    resolve_buy_ticker: Callable[[str], str],
    *,
    decision_reference: str | None,
    status: BrokerFactStatus,
    direct_fact: bool = False,
    side: Literal["buy", "sell"] = "buy",
    occurred_at: datetime | None = None,
    occurred_on: date | None = None,
    ticker: str | None = None,
    quantity: int | None = None,
    price_yen: Decimal | None = None,
    reservation_id: str | None = None,
    reservation_ids: tuple[str, ...] = (),
    order_id: str | None = None,
    execution_id: str | None = None,
    sector: str | None = None,
    common_factors: tuple[str, ...] = (),
    price_guard_yen: Decimal | None = None,
    expires_at: datetime | None = None,
    expiry_label: str | None = None,
    route_label: str | None = None,
    ordered_at: datetime | None = None,
    now: datetime | None = None,
) -> tuple[LedgerDraft | None, tuple[str, ...]]:
    """Build a draft from a saved buy binding or an explicitly reported direct fact.

    The saved decision binding identifies the buy ticker. Order terms are
    human-reported facts, separate from the published proposal. Existing reservations keep the same
    decision reference for every partial or terminal broker fact. The resolver
    is called only when no active or known terminal reservation binds the report.
    """
    if reservation_id is not None and reservation_ids:
        raise ValueError("provide reservation_id or reservation_ids, not both")
    if execution_id is not None:
        if status != "filled":
            raise ValueError("execution_id is only valid for a fill")
        _validate_execution_id(execution_id)
    requested_ids = reservation_ids or (() if reservation_id is None else (reservation_id,))
    if len(requested_ids) > 1 and status not in {"cancelled", "expired"}:
        raise ValueError("multiple reservation_ids require a terminal broker fact")

    source, expected_head = ledger_service.load_with_head()
    binding_addition: DecisionReferenceBinding | None = None
    if direct_fact:
        if side != "buy" or ticker is None:
            raise ValueError("direct-fact requires an explicit buy ticker")
        known = known_broker_reference(
            source,
            execution_id=execution_id,
            order_id=order_id,
            reservation_ids=requested_ids,
        )
        if known is not None:
            saved_reference, saved_reservation_id = known
            if decision_reference is not None and decision_reference != saved_reference:
                raise ValueError("decision_reference conflicts with the reported broker identity")
            decision_reference = saved_reference
            if not requested_ids and saved_reservation_id is not None:
                requested_ids = (saved_reservation_id,)
        else:
            if decision_reference is None:
                if (
                    execution_id is None
                    and order_id is None
                    and not requested_ids
                    and any(
                        isinstance(event, (ExecutionEvent, ReservationEvent))
                        and event.ticker == ticker
                        and event.side == "buy"
                        and (event.decision_reference or "").startswith("manual-fact-")
                        for event in source.events
                    )
                ):
                    raise ValueError(
                        "direct-fact report without broker identity needs its saved manual-fact "
                        "reference; select the existing fact or explicitly identify a new fact"
                    )
                decision_reference = f"manual-fact-{uuid4()}"
            binding_addition = DecisionReferenceBinding(
                reference=decision_reference,
                ticker=ticker,
                source_id=decision_reference,
            )
    elif decision_reference is None:
        raise ValueError("decision_reference is required unless direct-fact is explicit")
    assert decision_reference is not None
    if (occurred_at is None) == (occurred_on is None):
        raise ValueError("specify exactly one of occurred_at and occurred_on")
    if occurred_on is not None:
        if status != "filled" or len(requested_ids) != 1:
            raise ValueError("date-only broker fact requires one reserved fill")
        occurred_at = _date_only_execution_key(source, occurred_on, requested_ids[0])
    assert occurred_at is not None

    def record_single_fact() -> BrokerFactRecordingResult:
        return record_broker_fact(
            source,
            decision_reference=decision_reference,
            status=status,
            side=side,
            occurred_at=occurred_at,
            occurred_on=occurred_on,
            ticker=ticker,
            quantity=quantity,
            price_yen=price_yen,
            reservation_id=requested_ids[0] if requested_ids else None,
            order_id=order_id,
            execution_id=execution_id,
            sector=sector,
            common_factors=common_factors,
            price_guard_yen=price_guard_yen,
            expires_at=expires_at,
            expiry_label=expiry_label,
            route_label=route_label,
            ordered_at=ordered_at,
            now=now,
        )

    # A fully filled order is no longer active. Check a saved execution before
    # consulting the buy binding or looking for an active reservation.
    if (
        status == "filled"
        and execution_id is not None
        and any(
            isinstance(event, ExecutionEvent) and event.execution_id == execution_id
            for event in source.events
        )
    ):
        result = record_single_fact()
        assert not result.changed
        return None, ()
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
        if status == "filled" and any(item.side != side for item in selected):
            raise ValueError("broker fact side does not match the active order")
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
    elif side == "buy":
        if status in {"cancelled", "expired"}:
            raise ValueError(f"{status} requires an active reservation")
        bound_ticker = (
            binding_addition.ticker
            if binding_addition is not None
            else resolve_buy_ticker(decision_reference)
        )
        if ticker != bound_ticker:
            raise ValueError("broker fact ticker does not match the buy decision reference")
    elif status != "open":
        raise ValueError("sell broker fact requires an active order")

    if len(requested_ids) > 1:
        assert status in {"cancelled", "expired"}
        result = record_terminal_broker_facts(
            source,
            decision_reference=decision_reference,
            status=status,
            occurred_at=occurred_at,
            reservation_ids=requested_ids,
            now=now,
        )
    else:
        result = record_single_fact()
    if not result.changed:
        return None, ()
    return (
        LedgerDraft(
            kind="broker-fact",
            expected_head=expected_head,
            source=source,
            replacement=result.document,
            confirmation_required=True,
            binding_addition=binding_addition,
        ),
        result.event_ids,
    )


def known_broker_reference(
    source: PortfolioLedgerDocument,
    *,
    execution_id: str | None,
    order_id: str | None,
    reservation_ids: tuple[str, ...],
) -> tuple[str, str | None] | None:
    """Resolve identities before minting a reference; an order never identifies a fill."""
    matches = [
        event
        for event in source.events
        if (
            isinstance(event, ExecutionEvent)
            and execution_id is not None
            and event.execution_id == execution_id
        )
        or (
            isinstance(event, ReservationEvent)
            and (
                (order_id is not None and event.order_id == order_id)
                or event.reservation_id in reservation_ids
            )
        )
    ]
    if not matches:
        return None
    references = {event.decision_reference for event in matches}
    ids = {event.reservation_id for event in matches}
    if None in references or len(references) != 1:
        raise ValueError("reported broker identities have inconsistent or missing references")
    reference = next(iter(references))
    assert reference is not None
    if len(ids) != 1:
        if len(reservation_ids) > 1 and execution_id is None and order_id is None:
            return reference, None
        raise ValueError("reported broker identities refer to different orders")
    saved_id = next(iter(ids))
    if order_id is not None and saved_id is not None:
        order = next(
            (
                event
                for event in source.events
                if isinstance(event, ReservationEvent) and event.reservation_id == saved_id
            ),
            None,
        )
        if order is None or order.order_id != order_id:
            raise ValueError("order_id conflicts with the reported broker identity")
    return reference, saved_id


def build_event_draft(
    service: LedgerStoreService,
    event: HumanEvent,
) -> LedgerDraft:
    source, expected_head = service.load_with_head()
    raw = source.model_dump(mode="json")
    raw["events"] = sorted(
        [*raw["events"], event.model_dump(mode="json")],
        key=lambda item: datetime.fromisoformat(str(item["occurred_at"])),
    )
    raw["as_of"] = max(source.as_of, event.occurred_at).isoformat()
    replacement = PortfolioLedgerDocument.model_validate(raw)
    replay_events_through(replacement.events, replacement.as_of)
    return LedgerDraft(
        kind="event",
        expected_head=expected_head,
        source=source,
        replacement=replacement,
        confirmation_required=True,
    )


def build_override_draft(
    service: LedgerStoreService,
    override: HumanOverride,
) -> LedgerDraft:
    source, expected_head = service.load_with_head()
    if any(item.override_id == override.override_id for item in source.overrides):
        raise ValueError(f"duplicate override_id: {override.override_id}")
    replacement = source.model_copy(
        update={
            "overrides": tuple(
                sorted((*source.overrides, override), key=lambda item: item.override_id)
            )
        }
    )
    active_human_overrides(replacement.overrides, as_of=replacement.as_of)
    replay_events_through(replacement.events, replacement.as_of)
    return LedgerDraft(
        kind="override",
        expected_head=expected_head,
        source=source,
        replacement=replacement,
        confirmation_required=True,
    )


def build_meta_draft(
    service: LedgerStoreService,
    *,
    as_of: datetime,
    estimated_exit_tax_rate_bps: int | None,
    estimated_exit_tax_basis: str | None,
) -> LedgerDraft:
    source, expected_head = service.load_with_head()
    replacement = PortfolioLedgerDocument.model_validate(
        {
            **source.model_dump(mode="json"),
            "as_of": max(source.as_of, as_of).isoformat(),
            "estimated_exit_tax_rate_bps": estimated_exit_tax_rate_bps,
            "estimated_exit_tax_basis": estimated_exit_tax_basis,
        }
    )
    replay_events_through(replacement.events, replacement.as_of)
    return LedgerDraft(
        kind="meta",
        expected_head=expected_head,
        source=source,
        replacement=replacement,
        confirmation_required=True,
    )


def build_sell_execution_draft(
    service: LedgerStoreService,
    *,
    occurred_at: datetime | None = None,
    occurred_on: date | None = None,
    ticker: str,
    quantity: int,
    price_yen: Decimal,
    reservation_id: str | None = None,
    fees_yen: int | None = None,
    tax_yen: int | None = None,
    decision_reference: str | None = None,
    execution_id: str | None = None,
) -> LedgerDraft | None:
    """Record a human-reported sell fill as a source-bound execution draft.

    The sell consumes FIFO cost from current holdings; event replay
    rejects a quantity above the reconciled holding, a non board-lot quantity,
    and any confirmed fee or tax that overdraws available cash. Broker fees and
    the confirmed capital-gain tax are recorded as their own cost / tax events so
    realized proceeds and cost stay separable in replay. ``decision_reference``
    can retain the reported Investment Proposal reference. A matching
    ``execution_id`` and complete report returns None without a draft.
    """

    if (occurred_at is None) == (occurred_on is None):
        raise ValueError("specify exactly one of occurred_at and occurred_on")
    source, expected_head = service.load_with_head()
    if occurred_on is not None:
        occurred_at = _date_only_execution_key(source, occurred_on, reservation_id)
    assert occurred_at is not None
    if execution_id is not None:
        _validate_execution_id(execution_id)
    if reservation_id is not None and decision_reference is None:
        saved_order = next(
            (
                event
                for event in source.events
                if isinstance(event, ReservationEvent) and event.reservation_id == reservation_id
            ),
            None,
        )
        if saved_order is not None:
            decision_reference = saved_order.decision_reference
    if execution_id is not None:
        existing = next(
            (
                event
                for event in source.events
                if isinstance(event, ExecutionEvent) and event.execution_id == execution_id
            ),
            None,
        )
        if existing is not None:
            existing_suffix = existing.event_id.removeprefix("human-sell-")
            fee = next(
                (
                    event
                    for event in source.events
                    if event.event_id == f"human-sell-fee-{existing_suffix}"
                ),
                None,
            )
            tax = next(
                (
                    event
                    for event in source.events
                    if event.event_id == f"human-sell-tax-{existing_suffix}"
                ),
                None,
            )
            if not (
                existing.side == "sell"
                and existing.occurred_at == occurred_at
                and existing.occurred_on == occurred_on
                and existing.ticker == ticker
                and existing.quantity == quantity
                and existing.price_yen == price_yen
                and existing.reservation_id == reservation_id
                and existing.decision_reference == decision_reference
                and (fee.amount_yen if isinstance(fee, CostEvent) else None) == fees_yen
                and (tax.amount_yen if isinstance(tax, ConfirmedTaxEvent) else None) == tax_yen
            ):
                raise ValueError("conflicting human report for existing execution_id")
            return None
    elif occurred_on is not None and any(
        isinstance(event, ExecutionEvent)
        and event.side == "sell"
        and event.ticker == ticker
        and event.reservation_id == reservation_id
        and event.occurred_on == occurred_on
        for event in source.events
    ):
        raise ValueError("another date-only sell fill needs an execution_id to distinguish it")
    if reservation_id is not None:
        active = {
            item.reservation_id: item
            for item in reservation_snapshots(replay_events_through(source.events, source.as_of))
        }
        order = active.get(reservation_id)
        if order is None or order.side != "sell" or order.ticker != ticker:
            raise ValueError("reservation_id must identify an active sell order for ticker")
        if decision_reference is None:
            decision_reference = order.decision_reference
    suffix = _sell_event_suffix(
        occurred_at=occurred_at,
        ticker=ticker,
        quantity=quantity,
        price_yen=price_yen,
        decision_reference=decision_reference,
        reservation_id=reservation_id,
        occurred_on=occurred_on,
        execution_id=execution_id,
    )
    occurred_at_text = occurred_at.isoformat()
    additions: list[dict[str, object]] = [
        {
            "event_id": f"human-sell-{suffix}",
            "type": "execution",
            "occurred_at": occurred_at_text,
            "execution_id": execution_id or f"sell-execution-{suffix}",
            "reservation_id": reservation_id,
            "ticker": ticker,
            "side": "sell",
            "quantity": quantity,
            "price_yen": str(price_yen),
            "decision_reference": decision_reference,
            **({"occurred_on": occurred_on.isoformat()} if occurred_on is not None else {}),
        }
    ]
    if fees_yen is not None:
        additions.append(
            {
                "event_id": f"human-sell-fee-{suffix}",
                "type": "cost",
                "occurred_at": occurred_at_text,
                "ticker": ticker,
                "cost_kind": "commission",
                "amount_yen": fees_yen,
            }
        )
    if tax_yen is not None:
        additions.append(
            {
                "event_id": f"human-sell-tax-{suffix}",
                "type": "tax_confirmed",
                "occurred_at": occurred_at_text,
                "ticker": ticker,
                "tax_kind": "capital_gain",
                "amount_yen": tax_yen,
            }
        )
    raw = source.model_dump(mode="json")
    raw["events"] = sorted(
        [*raw["events"], *additions],
        key=lambda item: datetime.fromisoformat(str(item["occurred_at"])),
    )
    raw["as_of"] = max(source.as_of, occurred_at).isoformat()
    replacement = PortfolioLedgerDocument.model_validate(raw)
    replay_events_through(replacement.events, replacement.as_of)
    return LedgerDraft(
        kind="sell-execution",
        expected_head=expected_head,
        source=source,
        replacement=replacement,
        confirmation_required=True,
    )


def _date_only_execution_key(
    source: PortfolioLedgerDocument, occurred_on: date, reservation_id: str | None
) -> datetime:
    """Choose a replay key after the order without claiming an execution time."""

    day_start = datetime.combine(occurred_on, time.min, tzinfo=JST)
    if reservation_id is None:
        return day_start
    order = next(
        (
            event
            for event in source.events
            if isinstance(event, ReservationEvent) and event.reservation_id == reservation_id
        ),
        None,
    )
    if order is None:
        raise ValueError("date-only fill requires a saved reservation")
    replay_key = max(day_start, order.occurred_at)
    if replay_key.astimezone(JST).date() != occurred_on:
        raise ValueError("date-only fill predates the saved reservation")
    return replay_key


def _sell_event_suffix(
    *,
    occurred_at: datetime,
    ticker: str,
    quantity: int,
    price_yen: Decimal,
    decision_reference: str | None,
    reservation_id: str | None = None,
    occurred_on: date | None = None,
    execution_id: str | None = None,
) -> str:
    if execution_id is not None:
        return _provided_execution_suffix(execution_id)
    raw = "|".join(
        (
            decision_reference or "",
            ticker,
            str(quantity),
            str(price_yen),
            (
                f"date:{occurred_on.isoformat()}"
                if occurred_on is not None
                else occurred_at.isoformat()
            ),
        )
    )
    if reservation_id is not None:
        raw += f"|{reservation_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def write_draft(path: Path, draft: LedgerDraft) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        yaml.safe_dump(draft.model_dump(mode="json"), stream, sort_keys=False, allow_unicode=True)


def load_draft(path: Path) -> LedgerDraft:
    raw = safe_load(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and raw.get("schema_version") != 2:
        raise ValueError("ledger draft schema_version=2 required; prepare a new draft")
    return LedgerDraft.model_validate(raw)


def apply_draft(
    service: LedgerStoreService,
    draft: LedgerDraft,
    *,
    human_confirmed: bool,
) -> LedgerApplyResult:
    if not human_confirmed:
        raise ValueError("ledger draft apply requires explicit human confirmation")
    if not draft.confirmation_required:
        raise ValueError("ledger draft is not eligible for explicit apply")
    active_human_overrides(draft.replacement.overrides, as_of=draft.replacement.as_of)
    state = replay_events_through(draft.replacement.events, draft.replacement.as_of)
    if draft.kind == "broker-fact":
        require_resolved_expiries(state)
    return service.apply_document(
        expected_head=draft.expected_head,
        expected_document=draft.source,
        replacement=draft.replacement,
        binding_addition=draft.binding_addition,
    )


__all__ = [
    "DraftKind",
    "LedgerDraft",
    "apply_draft",
    "build_broker_fact_draft",
    "build_event_draft",
    "build_meta_draft",
    "build_override_draft",
    "build_sell_execution_draft",
    "known_broker_reference",
    "load_draft",
    "write_draft",
]
