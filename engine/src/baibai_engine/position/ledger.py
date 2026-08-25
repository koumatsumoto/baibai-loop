"""Repository-only portfolio ledger and deterministic snapshot reconciliation."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Literal, cast

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.position.policy import PORTFOLIO_POLICY, PolicyConfig

_MODEL_CONFIG = ConfigDict(frozen=True, strict=True, extra="forbid")
_TICKER_PATTERN = r"^[0-9A-Z]{4}$"


class PortfolioLedgerError(ValueError):
    """Raised when a ledger cannot be reconciled without inventing state."""


def _datetime(value: object) -> datetime:
    # Events reach the model both as YAML text and as an already-parsed instant from
    # the CLI, and both have to land on the same tz-aware value. Only the timezone
    # requirement below is a real constraint on the caller.
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError("must be an ISO datetime") from error
    else:
        raise ValueError("must be an ISO datetime")
    if parsed.tzinfo is None:
        raise ValueError("must include a timezone")
    return parsed


def _tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


def _price(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        raise ValueError("must be a decimal unit price")
    if isinstance(value, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]{1,4})?", value) is None:
        raise ValueError("string unit price must use fixed-point decimal notation")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("must be a decimal unit price") from error
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("must be a positive finite unit price")
    exponent = parsed.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -4:
        raise ValueError("unit price supports at most four decimal places")
    return parsed


class _EventBase(BaseModel):
    model_config = _MODEL_CONFIG

    event_id: Annotated[str, Field(min_length=1)]
    occurred_at: datetime

    @field_validator("occurred_at", mode="before")
    @classmethod
    def _parse_occurred_at(cls, value: object) -> datetime:
        return _datetime(value)


class OpeningBalanceEvent(_EventBase):
    type: Literal["opening_balance"]
    amount_yen: Annotated[int, Field(gt=0)]


class ContributionEvent(_EventBase):
    type: Literal["contribution"]
    amount_yen: Annotated[int, Field(gt=0)]


class WithdrawalEvent(_EventBase):
    type: Literal["withdrawal"]
    amount_yen: Annotated[int, Field(gt=0)]


class ReservationEvent(_EventBase):
    type: Literal["reservation"]
    reservation_id: Annotated[str, Field(min_length=1)]
    order_id: Annotated[str, Field(min_length=1)]
    ticker: Annotated[str, Field(pattern=_TICKER_PATTERN)]
    sector: Annotated[str, Field(min_length=1)]
    common_factors: tuple[Annotated[str, Field(min_length=1)], ...] = ()
    decision_reference: Annotated[str, Field(min_length=1)] | None = None
    quantity: Annotated[int, Field(gt=0)]
    price_guard_yen: Annotated[Decimal, Field(gt=0, decimal_places=4)]
    expires_at: datetime

    @field_validator("common_factors", mode="before")
    @classmethod
    def _parse_common_factors(cls, value: object) -> object:
        return _tuple(value)

    @field_validator("expires_at", mode="before")
    @classmethod
    def _parse_expires_at(cls, value: object) -> datetime:
        return _datetime(value)

    @field_validator("price_guard_yen", mode="before")
    @classmethod
    def _parse_price_guard(cls, value: object) -> Decimal:
        return _price(value)

    @model_validator(mode="after")
    def _expiry_follows_reservation(self) -> ReservationEvent:
        if self.expires_at <= self.occurred_at:
            raise ValueError("expires_at must be after occurred_at")
        if len(set(self.common_factors)) != len(self.common_factors):
            raise ValueError("common_factors must not contain duplicates")
        if self.common_factors != tuple(sorted(self.common_factors)):
            raise ValueError("common_factors must be sorted")
        return self


class ReleaseEvent(_EventBase):
    type: Literal["release"]
    reservation_id: Annotated[str, Field(min_length=1)]
    reason: Literal["cancelled", "expired", "broker_rejected", "decision_changed"]
    decision_reference: Annotated[str, Field(min_length=1)] | None = None


class ExecutionEvent(_EventBase):
    type: Literal["execution"]
    execution_id: Annotated[str, Field(min_length=1)]
    reservation_id: Annotated[str, Field(min_length=1)] | None = None
    ticker: Annotated[str, Field(pattern=_TICKER_PATTERN)]
    side: Literal["buy", "sell"]
    quantity: Annotated[int, Field(gt=0)]
    price_yen: Annotated[Decimal, Field(gt=0, decimal_places=4)]
    decision_reference: Annotated[str, Field(min_length=1)] | None = None

    @field_validator("price_yen", mode="before")
    @classmethod
    def _parse_price(cls, value: object) -> Decimal:
        return _price(value)


class IncomeEvent(_EventBase):
    type: Literal["income"]
    ticker: Annotated[str, Field(pattern=_TICKER_PATTERN)] | None = None
    income_kind: Literal["dividend", "other"]
    amount_yen: Annotated[int, Field(gt=0)]


class CostEvent(_EventBase):
    type: Literal["cost"]
    ticker: Annotated[str, Field(pattern=_TICKER_PATTERN)] | None = None
    cost_kind: Literal["commission", "exchange_fee", "other"]
    amount_yen: Annotated[int, Field(gt=0)]


class ConfirmedTaxEvent(_EventBase):
    type: Literal["tax_confirmed"]
    ticker: Annotated[str, Field(pattern=_TICKER_PATTERN)] | None = None
    tax_kind: Literal["dividend", "capital_gain", "other"]
    amount_yen: Annotated[int, Field(gt=0)]


type LedgerEvent = Annotated[
    OpeningBalanceEvent
    | ContributionEvent
    | WithdrawalEvent
    | ReservationEvent
    | ReleaseEvent
    | ExecutionEvent
    | IncomeEvent
    | CostEvent
    | ConfirmedTaxEvent,
    Field(discriminator="type"),
]


class MarketPrice(BaseModel):
    model_config = _MODEL_CONFIG

    ticker: Annotated[str, Field(pattern=_TICKER_PATTERN)]
    price_yen: Annotated[Decimal, Field(gt=0, decimal_places=4)]
    observed_at: datetime
    source_kind: Literal["market_api", "official_exchange", "licensed_dataset", "test_fixture"]
    price_basis: Literal["current", "close", "unadjusted_close"]
    source_ref: Annotated[str, Field(min_length=1)]

    @field_validator("observed_at", mode="before")
    @classmethod
    def _parse_observed_at(cls, value: object) -> datetime:
        return _datetime(value)

    @field_validator("price_yen", mode="before")
    @classmethod
    def _parse_price(cls, value: object) -> Decimal:
        return _price(value)


class HumanOverride(BaseModel):
    model_config = _MODEL_CONFIG

    override_id: Annotated[str, Field(min_length=1)]
    scope: Literal["ticker", "sector", "common_factor", "dry_powder"]
    key: Annotated[str, Field(min_length=1)]
    reason: Annotated[str, Field(min_length=1)]
    decision_reference: Annotated[str, Field(min_length=1)]
    approved_at: datetime
    expires_at: datetime

    @field_validator("approved_at", "expires_at", mode="before")
    @classmethod
    def _parse_times(cls, value: object) -> datetime:
        return _datetime(value)

    @model_validator(mode="after")
    def _expiry_follows_approval(self) -> HumanOverride:
        if self.expires_at <= self.approved_at:
            raise ValueError("override expires_at must be after approved_at")
        if self.scope == "dry_powder" and self.key != "portfolio":
            raise ValueError("dry_powder override key must be portfolio")
        return self


class PortfolioLedgerDocument(BaseModel):
    """Versioned persistence contract for repository-only portfolio capital."""

    model_config = _MODEL_CONFIG

    schema_version: Literal[2]
    portfolio_scope: Literal["repository_only"]
    as_of: datetime
    estimated_exit_tax_rate_bps: Annotated[int, Field(ge=0, le=10_000)] | None = None
    estimated_exit_tax_basis: Literal["ledger_fifo_gross_unrealized_gain"] | None = None
    events: tuple[LedgerEvent, ...]
    market_prices: tuple[MarketPrice, ...]
    overrides: tuple[HumanOverride, ...] = ()

    @field_validator("as_of", mode="before")
    @classmethod
    def _parse_as_of(cls, value: object) -> datetime:
        return _datetime(value)

    @field_validator("events", "market_prices", "overrides", mode="before")
    @classmethod
    def _parse_sequences(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def _validate_document_order(self) -> PortfolioLedgerDocument:
        if not self.events or not isinstance(self.events[0], OpeningBalanceEvent):
            raise ValueError("the first event must be opening_balance")
        opening_count = sum(isinstance(event, OpeningBalanceEvent) for event in self.events)
        if opening_count != 1:
            raise ValueError("the ledger must contain exactly one opening_balance")
        event_ids = [event.event_id for event in self.events]
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("event_id must be unique")
        if any(
            current.occurred_at > following.occurred_at
            for current, following in zip(self.events, self.events[1:], strict=False)
        ):
            raise ValueError("events must be ordered by occurred_at")
        if any(event.occurred_at > self.as_of for event in self.events):
            raise ValueError("events cannot occur after as_of")
        price_tickers = [price.ticker for price in self.market_prices]
        if len(set(price_tickers)) != len(price_tickers):
            raise ValueError("market_prices ticker must be unique")
        if any(price.observed_at > self.as_of for price in self.market_prices):
            raise ValueError("market price cannot be observed after as_of")
        override_ids = [override.override_id for override in self.overrides]
        if len(set(override_ids)) != len(override_ids):
            raise ValueError("override_id must be unique")
        if any(override.approved_at > self.as_of for override in self.overrides):
            raise ValueError("override approval cannot occur after as_of")
        if (self.estimated_exit_tax_rate_bps is None) != (self.estimated_exit_tax_basis is None):
            raise ValueError("exit tax rate and basis must be specified together")
        return self


@dataclass(frozen=True, slots=True)
class HoldingSnapshot:
    ticker: str
    sector: str
    common_factors: tuple[str, ...]
    quantity: int
    deployed_cost_yen: int
    market_price_yen: Decimal
    market_price_observed_at: datetime
    market_price_source_kind: str
    market_price_basis: str
    market_price_source_ref: str
    market_value_yen: int


@dataclass(frozen=True, slots=True)
class ReservationSnapshot:
    reservation_id: str
    order_id: str
    ticker: str
    sector: str
    common_factors: tuple[str, ...]
    decision_reference: str | None
    remaining_quantity: int
    price_guard_yen: Decimal
    reserved_yen: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class PortfolioWarning:
    code: str
    scope: str
    key: str
    actual_pct: float
    warning_pct: float
    overridden: bool
    override_id: str | None = None
    override_reason: str | None = None
    override_expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    as_of: datetime
    portfolio_scope: Literal["repository_only"]
    available_cash_yen: int
    reserved_cash_yen: int
    deployed_cost_yen: int
    holdings_market_value_yen: int
    confirmed_income_yen: int
    confirmed_cost_yen: int
    confirmed_tax_yen: int
    confirmed_cost_tax_yen: int
    book_capital_yen: int
    total_capital_yen: int
    estimated_exit_tax_rate_bps: int | None
    estimated_exit_tax_basis: str | None
    estimated_exit_tax_yen: int | None
    holdings: tuple[HoldingSnapshot, ...]
    active_reservations: tuple[ReservationSnapshot, ...]
    warnings: tuple[PortfolioWarning, ...]


@dataclass(slots=True)
class _Reservation:
    reservation_id: str
    order_id: str
    ticker: str
    sector: str
    common_factors: tuple[str, ...]
    decision_reference: str | None
    remaining_quantity: int
    price_guard_yen: Decimal
    expires_at: datetime


@dataclass(slots=True)
class _Lot:
    quantity: int
    price_yen: Decimal


@dataclass(slots=True)
class ReplayedPortfolioState:
    """Price-independent ledger state at one instant.

    Historical outcome measurement reuses this state with a close price for the
    valuation date. Keeping execution replay separate from valuation prevents a
    final ledger quote or a current override from leaking into past NAV.
    """

    as_of: datetime
    available_cash_yen: int
    reserved_cash_yen: int
    confirmed_income_yen: int
    confirmed_cost_yen: int
    confirmed_tax_yen: int
    active_reservations: dict[str, _Reservation]
    lots: dict[str, list[_Lot]]
    metadata: dict[str, tuple[str, tuple[str, ...]]]


@dataclass(frozen=True, slots=True)
class ReplayedPortfolioValue:
    available_cash_yen: int
    reserved_cash_yen: int
    deployed_cost_yen: int
    holdings_market_value_yen: int
    total_capital_yen: int
    holdings: tuple[HoldingSnapshot, ...]


def load_portfolio_ledger_with_sha256(path: Path) -> tuple[PortfolioLedgerDocument, str]:
    """Parse a ledger and hash the exact same bytes used for that parse."""

    try:
        source = path.read_bytes()
        text = source.decode("utf-8")
        raw = safe_load(text)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise PortfolioLedgerError(f"failed to read ledger: {error}") from error
    if not isinstance(raw, Mapping):
        raise PortfolioLedgerError("portfolio ledger root must be a mapping")
    try:
        document = PortfolioLedgerDocument.model_validate(raw)
    except ValidationError as error:
        raise PortfolioLedgerError(str(error)) from error
    return document, hashlib.sha256(source).hexdigest()


def replay_events_through(
    events: tuple[LedgerEvent, ...],
    as_of: datetime,
    *,
    policy: PolicyConfig = PORTFOLIO_POLICY,
) -> ReplayedPortfolioState:
    """Replay ordered events through ``as_of`` without reading any price.

    Callers must preserve the ledger input sequence: events sharing an instant
    are intentionally not re-sorted by identifier.  This is the same execution
    state machine used by the current ledger snapshot, exposed so historical
    valuation can supply only that day's close prices.

    What this enforces are the ledger's own arithmetic invariants — identifiers,
    board lots, price guards, FIFO, cash sufficiency, reserved-cash reconciliation.
    Whether a lapsed reservation still awaiting its release is acceptable depends
    on what the caller is describing, not on the events, so that question lives
    with the caller: see ``require_resolved_expiries``.
    """

    available_cash = reserved_cash = confirmed_income = confirmed_cost = confirmed_tax = 0
    active: dict[str, _Reservation] = {}
    seen_reservations: set[str] = set()
    seen_order_ids: set[str] = set()
    seen_executions: set[str] = set()
    lots: dict[str, list[_Lot]] = defaultdict(list)
    metadata: dict[str, tuple[str, tuple[str, ...]]] = {}
    board_lot = _policy_positive_int(_policy_mapping(policy, "order_constraints"), "board_lot")

    for event in events:
        if event.occurred_at > as_of:
            continue
        match event:
            case OpeningBalanceEvent():
                available_cash += event.amount_yen
            case ContributionEvent():
                available_cash += event.amount_yen
            case WithdrawalEvent():
                _require_cash(available_cash, event.amount_yen, event.event_id)
                available_cash -= event.amount_yen
            case ReservationEvent():
                if event.reservation_id in seen_reservations:
                    raise PortfolioLedgerError(
                        f"reservation_id already used: {event.reservation_id}"
                    )
                if event.order_id in seen_order_ids:
                    raise PortfolioLedgerError(f"order_id already reserved: {event.order_id}")
                if event.quantity % board_lot:
                    raise PortfolioLedgerError(
                        f"reservation quantity must be a multiple of board_lot {board_lot}"
                    )
                _yen_notional(
                    board_lot, event.price_guard_yen, field="reservation board-lot notional"
                )
                notional = _yen_notional(
                    event.quantity, event.price_guard_yen, field="reservation notional"
                )
                _require_cash(available_cash, notional, event.event_id)
                has_exposure = any(lot.quantity > 0 for lot in lots[event.ticker]) or any(
                    item.ticker == event.ticker for item in active.values()
                )
                if not has_exposure:
                    metadata.pop(event.ticker, None)
                _check_metadata(metadata, event.ticker, event.sector, event.common_factors)
                seen_reservations.add(event.reservation_id)
                seen_order_ids.add(event.order_id)
                active[event.reservation_id] = _Reservation(
                    reservation_id=event.reservation_id,
                    order_id=event.order_id,
                    ticker=event.ticker,
                    sector=event.sector,
                    common_factors=event.common_factors,
                    decision_reference=event.decision_reference,
                    remaining_quantity=event.quantity,
                    price_guard_yen=event.price_guard_yen,
                    expires_at=event.expires_at,
                )
                available_cash -= notional
                reserved_cash += notional
            case ReleaseEvent():
                reservation = active.get(event.reservation_id)
                if reservation is None:
                    raise PortfolioLedgerError(
                        f"release references no active reservation: {event.reservation_id}"
                    )
                if event.reason == "expired" and event.occurred_at < reservation.expires_at:
                    raise PortfolioLedgerError(
                        f"expired release predates expires_at: {event.reservation_id}"
                    )
                if event.occurred_at >= reservation.expires_at and event.reason != "expired":
                    raise PortfolioLedgerError(
                        "release at or after expires_at must use expired reason: "
                        f"{event.reservation_id}"
                    )
                released = _yen_notional(
                    reservation.remaining_quantity,
                    reservation.price_guard_yen,
                    field="released reservation notional",
                )
                available_cash += released
                reserved_cash -= released
                del active[event.reservation_id]
            case ExecutionEvent(side="buy"):
                if event.execution_id in seen_executions:
                    raise PortfolioLedgerError(f"execution_id already used: {event.execution_id}")
                if event.reservation_id is None:
                    raise PortfolioLedgerError("buy execution requires reservation_id")
                if event.quantity % board_lot:
                    raise PortfolioLedgerError(
                        f"execution quantity must be a multiple of board_lot {board_lot}"
                    )
                reservation = active.get(event.reservation_id)
                if reservation is None:
                    raise PortfolioLedgerError(
                        f"buy references no active reservation: {event.reservation_id}"
                    )
                if event.ticker != reservation.ticker:
                    raise PortfolioLedgerError("execution ticker must match reservation ticker")
                if event.occurred_at >= reservation.expires_at:
                    raise PortfolioLedgerError("buy execution cannot occur at or after expires_at")
                if event.quantity > reservation.remaining_quantity:
                    raise PortfolioLedgerError("execution quantity exceeds reserved quantity")
                if event.price_yen > reservation.price_guard_yen:
                    raise PortfolioLedgerError("execution price exceeds reservation price guard")
                seen_executions.add(event.execution_id)
                guarded = _yen_notional(
                    event.quantity, reservation.price_guard_yen, field="filled guarded notional"
                )
                paid = _yen_notional(
                    event.quantity, event.price_yen, field="buy execution notional"
                )
                reserved_cash -= guarded
                available_cash += guarded - paid
                reservation.remaining_quantity -= event.quantity
                lots[event.ticker].append(_Lot(event.quantity, event.price_yen))
                if reservation.remaining_quantity == 0:
                    del active[event.reservation_id]
            case ExecutionEvent(side="sell"):
                if event.execution_id in seen_executions:
                    raise PortfolioLedgerError(f"execution_id already used: {event.execution_id}")
                if event.reservation_id is not None:
                    raise PortfolioLedgerError("sell execution cannot consume a cash reservation")
                if event.quantity % board_lot:
                    raise PortfolioLedgerError(
                        f"execution quantity must be a multiple of board_lot {board_lot}"
                    )
                _consume_fifo(lots[event.ticker], event.quantity, event.ticker)
                seen_executions.add(event.execution_id)
                available_cash += _yen_notional(
                    event.quantity, event.price_yen, field="sell execution notional"
                )
            case IncomeEvent():
                available_cash += event.amount_yen
                confirmed_income += event.amount_yen
            case CostEvent():
                _require_cash(available_cash, event.amount_yen, event.event_id)
                available_cash -= event.amount_yen
                confirmed_cost += event.amount_yen
            case ConfirmedTaxEvent():
                _require_cash(available_cash, event.amount_yen, event.event_id)
                available_cash -= event.amount_yen
                confirmed_tax += event.amount_yen

    if reserved_cash != sum(
        _yen_notional(
            item.remaining_quantity, item.price_guard_yen, field="active reservation notional"
        )
        for item in active.values()
    ):
        raise PortfolioLedgerError("reserved cash does not reconcile to active reservations")
    return ReplayedPortfolioState(
        as_of=as_of,
        available_cash_yen=available_cash,
        reserved_cash_yen=reserved_cash,
        confirmed_income_yen=confirmed_income,
        confirmed_cost_yen=confirmed_cost,
        confirmed_tax_yen=confirmed_tax,
        active_reservations=active,
        lots=lots,
        metadata=metadata,
    )


def require_resolved_expiries(state: ReplayedPortfolioState) -> None:
    """Reject a state that still owes a human's report on a lapsed reservation.

    This is a claim about the ledger being current, not about the events being
    well-formed: a reservation whose ``expires_at`` has passed with no release is
    exactly what the ledger looks like between the lapse and the evening the human
    reports it. A snapshot that says "this is the capital right now" must not be
    built from that gap, so ``reconcile_portfolio`` calls this. A historical
    valuation replaying a prefix legitimately passes through the gap and does not.
    """

    expired = sorted(
        item.reservation_id
        for item in state.active_reservations.values()
        if item.expires_at <= state.as_of
    )
    if expired:
        raise PortfolioLedgerError(f"expired reservations require an explicit release: {expired}")


def value_replayed_state(
    state: ReplayedPortfolioState,
    prices_at_close: Mapping[str, MarketPrice],
) -> ReplayedPortfolioValue:
    """Value a replayed state using only close prices provided for that date."""

    holdings = _holding_snapshots(state.lots, state.metadata, prices_at_close)
    deployed_cost = sum(holding.deployed_cost_yen for holding in holdings)
    holdings_market_value = sum(holding.market_value_yen for holding in holdings)
    total_capital = state.available_cash_yen + state.reserved_cash_yen + holdings_market_value
    return ReplayedPortfolioValue(
        available_cash_yen=state.available_cash_yen,
        reserved_cash_yen=state.reserved_cash_yen,
        deployed_cost_yen=deployed_cost,
        holdings_market_value_yen=holdings_market_value,
        total_capital_yen=total_capital,
        holdings=holdings,
    )


def reservation_snapshots(state: ReplayedPortfolioState) -> tuple[ReservationSnapshot, ...]:
    """Expose active reservation facts from price-free event replay."""

    return _reservation_snapshots(state.active_reservations)


def _reservation_snapshots(
    active: Mapping[str, _Reservation],
) -> tuple[ReservationSnapshot, ...]:
    return tuple(
        ReservationSnapshot(
            reservation_id=item.reservation_id,
            order_id=item.order_id,
            ticker=item.ticker,
            sector=item.sector,
            common_factors=item.common_factors,
            decision_reference=item.decision_reference,
            remaining_quantity=item.remaining_quantity,
            price_guard_yen=item.price_guard_yen,
            reserved_yen=_yen_notional(
                item.remaining_quantity,
                item.price_guard_yen,
                field="reservation snapshot notional",
            ),
            expires_at=item.expires_at,
        )
        for item in sorted(active.values(), key=lambda item: item.reservation_id)
    )


def reconcile_portfolio(
    document: PortfolioLedgerDocument,
    *,
    policy: PolicyConfig = PORTFOLIO_POLICY,
) -> PortfolioSnapshot:
    """Replay the ledger once and derive cash, holdings, valuation, and warnings.

    The event state machine and its invariants live in ``replay_events_through``.
    The document validator already rejects events after ``as_of``, so replaying
    the whole event tuple through ``as_of`` reaches the same terminal state.
    """

    # The replay skips anything after as_of, so a document carrying such an event would
    # be reconciled from a silently truncated history. The document validator rejects
    # that shape, and stating it here keeps the delegation from depending on a guarantee
    # made somewhere else.
    if document.events and document.events[-1].occurred_at > document.as_of:
        raise PortfolioLedgerError("events cannot occur after as_of")
    state = replay_events_through(document.events, document.as_of, policy=policy)
    require_resolved_expiries(state)
    valuation_policy = _policy_mapping(policy, "valuation")
    max_price_age_days = _policy_positive_int(valuation_policy, "market_price_max_age_days")
    for price in document.market_prices:
        age_days = (document.as_of - price.observed_at).total_seconds() / 86_400
        if age_days > max_price_age_days:
            raise PortfolioLedgerError(
                f"market price for {price.ticker} is stale: {age_days:.2f} days old"
            )
    prices = {price.ticker: price for price in document.market_prices}
    value = value_replayed_state(state, prices)
    holdings = value.holdings
    reservations = reservation_snapshots(state)
    available_cash = state.available_cash_yen
    reserved_cash = state.reserved_cash_yen
    book_capital = available_cash + reserved_cash + value.deployed_cost_yen
    total_capital = value.total_capital_yen
    if total_capital <= 0:
        raise PortfolioLedgerError("total capital must remain positive")
    estimated_tax = estimated_exit_tax_yen(
        holdings,
        rate_bps=document.estimated_exit_tax_rate_bps,
        basis=document.estimated_exit_tax_basis,
    )
    warnings = _portfolio_warnings(
        document,
        holdings=holdings,
        reservations=reservations,
        available_cash=available_cash,
        total_capital=total_capital,
        policy=policy,
    )
    return PortfolioSnapshot(
        as_of=document.as_of,
        portfolio_scope=document.portfolio_scope,
        available_cash_yen=available_cash,
        reserved_cash_yen=reserved_cash,
        deployed_cost_yen=value.deployed_cost_yen,
        holdings_market_value_yen=value.holdings_market_value_yen,
        confirmed_income_yen=state.confirmed_income_yen,
        confirmed_cost_yen=state.confirmed_cost_yen,
        confirmed_tax_yen=state.confirmed_tax_yen,
        confirmed_cost_tax_yen=state.confirmed_cost_yen + state.confirmed_tax_yen,
        book_capital_yen=book_capital,
        total_capital_yen=total_capital,
        estimated_exit_tax_rate_bps=document.estimated_exit_tax_rate_bps,
        estimated_exit_tax_basis=document.estimated_exit_tax_basis,
        estimated_exit_tax_yen=estimated_tax,
        holdings=holdings,
        active_reservations=reservations,
        warnings=warnings,
    )


def snapshot_to_payload(snapshot: PortfolioSnapshot) -> dict[str, object]:
    """Convert a snapshot to deterministic YAML/JSON-ready primitives."""

    return {
        "as_of": snapshot.as_of.isoformat(),
        "portfolio_scope": snapshot.portfolio_scope,
        "available_cash_yen": snapshot.available_cash_yen,
        "reserved_cash_yen": snapshot.reserved_cash_yen,
        "deployed_cost_yen": snapshot.deployed_cost_yen,
        "holdings_market_value_yen": snapshot.holdings_market_value_yen,
        "confirmed_income_yen": snapshot.confirmed_income_yen,
        "confirmed_cost_yen": snapshot.confirmed_cost_yen,
        "confirmed_tax_yen": snapshot.confirmed_tax_yen,
        "confirmed_cost_tax_yen": snapshot.confirmed_cost_tax_yen,
        "book_capital_yen": snapshot.book_capital_yen,
        "total_capital_yen": snapshot.total_capital_yen,
        "estimated_exit_tax_rate_bps": snapshot.estimated_exit_tax_rate_bps,
        "estimated_exit_tax_basis": snapshot.estimated_exit_tax_basis,
        "estimated_exit_tax_yen": snapshot.estimated_exit_tax_yen,
        "holdings": [
            {
                "ticker": holding.ticker,
                "sector": holding.sector,
                "common_factors": list(holding.common_factors),
                "quantity": holding.quantity,
                "deployed_cost_yen": holding.deployed_cost_yen,
                "market_price_yen": _price_payload(holding.market_price_yen),
                "market_price_observed_at": holding.market_price_observed_at.isoformat(),
                "market_price_source_kind": holding.market_price_source_kind,
                "market_price_basis": holding.market_price_basis,
                "market_price_source_ref": holding.market_price_source_ref,
                "market_value_yen": holding.market_value_yen,
            }
            for holding in snapshot.holdings
        ],
        "active_reservations": [
            {
                "reservation_id": reservation.reservation_id,
                "order_id": reservation.order_id,
                "ticker": reservation.ticker,
                "sector": reservation.sector,
                "common_factors": list(reservation.common_factors),
                "remaining_quantity": reservation.remaining_quantity,
                "price_guard_yen": _price_payload(reservation.price_guard_yen),
                "reserved_yen": reservation.reserved_yen,
                "expires_at": reservation.expires_at.isoformat(),
            }
            for reservation in snapshot.active_reservations
        ],
        "warnings": [
            {
                "code": warning.code,
                "scope": warning.scope,
                "key": warning.key,
                "actual_pct": warning.actual_pct,
                "warning_pct": warning.warning_pct,
                "overridden": warning.overridden,
                "override_id": warning.override_id,
                "override_reason": warning.override_reason,
                "override_expires_at": (
                    warning.override_expires_at.isoformat()
                    if warning.override_expires_at is not None
                    else None
                ),
            }
            for warning in snapshot.warnings
        ],
    }


def _require_cash(available: int, required: int, event_id: str) -> None:
    if required > available:
        raise PortfolioLedgerError(
            f"insufficient available cash at {event_id}: required {required}, available {available}"
        )


def _yen_notional(quantity: int, price: Decimal, *, field: str) -> int:
    notional = price * quantity
    if notional != notional.to_integral_value():
        raise PortfolioLedgerError(f"{field} must reconcile to whole yen")
    return int(notional)


def _price_payload(price: Decimal) -> int | float:
    if price == price.to_integral_value():
        return int(price)
    return float(price)


def _check_metadata(
    metadata: dict[str, tuple[str, tuple[str, ...]]],
    ticker: str,
    sector: str,
    common_factors: tuple[str, ...],
) -> None:
    observed = (sector, common_factors)
    existing = metadata.setdefault(ticker, observed)
    if existing != observed:
        raise PortfolioLedgerError(f"inconsistent sector/common_factors for ticker {ticker}")


def _consume_fifo(lots: list[_Lot], quantity: int, ticker: str) -> None:
    available = sum(lot.quantity for lot in lots)
    if quantity > available:
        raise PortfolioLedgerError(
            f"sell quantity exceeds repository holding for {ticker}: {quantity} > {available}"
        )
    remaining = quantity
    while remaining:
        lot = lots[0]
        consumed = min(lot.quantity, remaining)
        lot.quantity -= consumed
        remaining -= consumed
        if lot.quantity == 0:
            lots.pop(0)


def _holding_snapshots(
    lots: Mapping[str, list[_Lot]],
    metadata: Mapping[str, tuple[str, tuple[str, ...]]],
    prices: Mapping[str, MarketPrice],
) -> tuple[HoldingSnapshot, ...]:
    holdings: list[HoldingSnapshot] = []
    for ticker, ticker_lots in sorted(lots.items()):
        quantity = sum(lot.quantity for lot in ticker_lots)
        if quantity == 0:
            continue
        if ticker not in prices:
            raise PortfolioLedgerError(f"market price is required for holding {ticker}")
        sector, factors = metadata[ticker]
        deployed = sum(
            _yen_notional(lot.quantity, lot.price_yen, field="holding deployed cost")
            for lot in ticker_lots
        )
        market_price = prices[ticker]
        holdings.append(
            HoldingSnapshot(
                ticker=ticker,
                sector=sector,
                common_factors=factors,
                quantity=quantity,
                deployed_cost_yen=deployed,
                market_price_yen=market_price.price_yen,
                market_price_observed_at=market_price.observed_at,
                market_price_source_kind=market_price.source_kind,
                market_price_basis=market_price.price_basis,
                market_price_source_ref=market_price.source_ref,
                market_value_yen=_yen_notional(
                    quantity, market_price.price_yen, field="holding market value"
                ),
            )
        )
    return tuple(holdings)


def estimated_exit_tax_yen(
    holdings: tuple[HoldingSnapshot, ...], *, rate_bps: int | None, basis: str | None
) -> int | None:
    """Return the configured FIFO gross-unrealized-gain tax estimate.

    Both the ledger aggregate and a holding review use this deliberately small
    estimate.  It is not an account-tax engine: confirmed tax, fees, loss
    offsets, and account type remain separate ledger facts.
    """
    if rate_bps is None or basis is None:
        return None
    if basis != "ledger_fifo_gross_unrealized_gain":
        raise ValueError(f"unsupported estimated exit tax basis: {basis}")
    unrealized_gain = sum(
        max(0, holding.market_value_yen - holding.deployed_cost_yen) for holding in holdings
    )
    return estimated_exit_tax_for_gain_yen(
        gross_unrealized_gain_yen=unrealized_gain,
        rate_bps=rate_bps,
        basis=basis,
    )


def estimated_exit_tax_for_gain_yen(
    *,
    gross_unrealized_gain_yen: int,
    rate_bps: int | None,
    basis: str | None,
) -> int | None:
    """Apply the ledger's configured future-exit-tax estimate to one gain."""
    if rate_bps is None or basis is None:
        return None
    if gross_unrealized_gain_yen < 0:
        raise ValueError("gross unrealized gain must not be negative")
    if basis != "ledger_fifo_gross_unrealized_gain":
        raise ValueError(f"unsupported estimated exit tax basis: {basis}")
    return gross_unrealized_gain_yen * rate_bps // 10_000


def _portfolio_warnings(
    document: PortfolioLedgerDocument,
    *,
    holdings: tuple[HoldingSnapshot, ...],
    reservations: tuple[ReservationSnapshot, ...],
    available_cash: int,
    total_capital: int,
    policy: PolicyConfig,
) -> tuple[PortfolioWarning, ...]:
    cash_policy = _policy_mapping(policy, "cash_management")
    risk_policy = _policy_mapping(policy, "risk_budget")
    max_override_days = _policy_number(cash_policy, "override_max_days")
    active_overrides: dict[tuple[str, str], HumanOverride] = {}
    for override in document.overrides:
        duration_days = (override.expires_at - override.approved_at).total_seconds() / 86_400
        if duration_days > max_override_days:
            raise PortfolioLedgerError(
                f"override {override.override_id} exceeds {max_override_days:g} days"
            )
        if override.approved_at <= document.as_of < override.expires_at:
            key = (override.scope, override.key)
            if key in active_overrides:
                raise PortfolioLedgerError(f"multiple active overrides for {key}")
            active_overrides[key] = override

    ticker_exposure: dict[str, int] = defaultdict(int)
    sector_exposure: dict[str, int] = defaultdict(int)
    factor_exposure: dict[str, int] = defaultdict(int)
    for holding in holdings:
        ticker_exposure[holding.ticker] += holding.market_value_yen
        sector_exposure[holding.sector] += holding.market_value_yen
        for factor in holding.common_factors:
            factor_exposure[factor] += holding.market_value_yen
    for reservation in reservations:
        ticker_exposure[reservation.ticker] += reservation.reserved_yen
        sector_exposure[reservation.sector] += reservation.reserved_yen
        for factor in reservation.common_factors:
            factor_exposure[factor] += reservation.reserved_yen

    warnings: list[PortfolioWarning] = []
    warning_specs = (
        ("ticker", ticker_exposure, "max_ticker_concentration_pct"),
        ("sector", sector_exposure, "max_sector_concentration_pct"),
        ("common_factor", factor_exposure, "max_common_factor_concentration_pct"),
    )
    for scope, exposures, policy_key in warning_specs:
        warning_pct = _policy_number(risk_policy, policy_key)
        for scope_key, exposure in sorted(exposures.items()):
            raw_actual_pct = exposure * 100 / total_capital
            actual_pct = round(raw_actual_pct, 2)
            if raw_actual_pct > warning_pct:
                warnings.append(
                    _warning(
                        code=f"portfolio.{scope}-concentration",
                        scope=scope,
                        key=scope_key,
                        actual_pct=actual_pct,
                        warning_pct=warning_pct,
                        overrides=active_overrides,
                    )
                )

    raw_dry_powder_pct = available_cash * 100 / total_capital
    dry_powder_pct = round(raw_dry_powder_pct, 2)
    dry_powder_warning = _policy_number(cash_policy, "dry_powder_warning_pct")
    if raw_dry_powder_pct < dry_powder_warning:
        warnings.append(
            _warning(
                code="portfolio.dry-powder",
                scope="dry_powder",
                key="portfolio",
                actual_pct=dry_powder_pct,
                warning_pct=dry_powder_warning,
                overrides=active_overrides,
            )
        )
    return tuple(warnings)


def _warning(
    *,
    code: str,
    scope: str,
    key: str,
    actual_pct: float,
    warning_pct: float,
    overrides: Mapping[tuple[str, str], HumanOverride],
) -> PortfolioWarning:
    override = overrides.get((scope, key))
    return PortfolioWarning(
        code=code,
        scope=scope,
        key=key,
        actual_pct=actual_pct,
        warning_pct=warning_pct,
        overridden=override is not None,
        override_id=override.override_id if override else None,
        override_reason=override.reason if override else None,
        override_expires_at=override.expires_at if override else None,
    )


def _policy_mapping(policy: PolicyConfig, key: str) -> Mapping[str, object]:
    value = policy.get(key)
    if not isinstance(value, Mapping):
        raise PortfolioLedgerError(f"policy.{key} must be a mapping")
    return cast(Mapping[str, object], value)


def _policy_number(policy: Mapping[str, object], key: str) -> float:
    value = policy.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PortfolioLedgerError(f"policy.{key} must be numeric")
    return float(value)


def _policy_positive_int(policy: Mapping[str, object], key: str) -> int:
    value = policy.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PortfolioLedgerError(f"policy.{key} must be a positive integer")
    return value
