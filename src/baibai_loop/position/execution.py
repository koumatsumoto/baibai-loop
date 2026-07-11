"""Strict lifecycle contract for human-approved manual equity execution."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from baibai_loop.foundation.yaml_io import safe_load
from baibai_loop.position.ledger import (
    ExecutionEvent,
    PortfolioLedgerDocument,
    PortfolioLedgerError,
    ReleaseEvent,
    ReservationEvent,
    reconcile_portfolio,
)

_MODEL_CONFIG = ConfigDict(frozen=True, strict=True, extra="forbid")
_TICKER_PATTERN = r"^[0-9A-Z]{4}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ExecutionLifecycleError(ValueError):
    """Raised when an execution lifecycle cannot be reconstructed safely."""


def _datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("must be an ISO datetime") from error
    if parsed.tzinfo is None:
        raise ValueError("must include a timezone")
    return parsed


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


def _tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


class DecisionIntent(BaseModel):
    """A human-confirmed execution instruction bound to one decision packet."""

    model_config = _MODEL_CONFIG

    intent_id: Annotated[str, Field(min_length=1)]
    decision_reference: Annotated[str, Field(min_length=1)]
    decision_packet_sha256: Annotated[str, Field(pattern=_SHA256_PATTERN)]
    confirmed_at: datetime
    side: Literal["buy", "sell"]
    quantity: Annotated[int, Field(gt=0)]
    price_guard_yen: Annotated[Decimal, Field(gt=0, decimal_places=4)]
    expires_at: datetime
    uses_margin: Literal[False]

    @field_validator("confirmed_at", "expires_at", mode="before")
    @classmethod
    def _parse_time(cls, value: object) -> datetime:
        return _datetime(value)

    @field_validator("price_guard_yen", mode="before")
    @classmethod
    def _parse_price(cls, value: object) -> Decimal:
        return _price(value)

    @model_validator(mode="after")
    def _valid_window(self) -> DecisionIntent:
        if self.expires_at <= self.confirmed_at:
            raise ValueError("expires_at must be after confirmed_at")
        return self


class BrokerOrder(BaseModel):
    """A manually submitted broker order under one decision intent."""

    model_config = _MODEL_CONFIG

    order_id: Annotated[str, Field(min_length=1)]
    origin_intent_id: Annotated[str, Field(min_length=1)]
    external_broker_order_id: Annotated[str, Field(min_length=1)] | None
    submitted_at: datetime
    submitted_quantity: Annotated[int, Field(gt=0)]
    limit_price_yen: Annotated[Decimal, Field(gt=0, decimal_places=4)]
    terminal_status: Literal["broker_rejected", "cancelled", "expired"] | None
    terminal_at: datetime | None

    @field_validator("submitted_at", "terminal_at", mode="before")
    @classmethod
    def _parse_time(cls, value: object) -> datetime | None:
        return None if value is None else _datetime(value)

    @field_validator("limit_price_yen", mode="before")
    @classmethod
    def _parse_price(cls, value: object) -> Decimal:
        return _price(value)

    @model_validator(mode="after")
    def _terminal_pair(self) -> BrokerOrder:
        if (self.terminal_status is None) != (self.terminal_at is None):
            raise ValueError("terminal_status and terminal_at must be specified together")
        if self.terminal_at is not None and self.terminal_at < self.submitted_at:
            raise ValueError("terminal_at cannot predate submitted_at")
        return self


class BrokerExecution(BaseModel):
    """An immutable execution fact confirmed by the broker."""

    model_config = _MODEL_CONFIG

    execution_id: Annotated[str, Field(min_length=1)]
    order_id: Annotated[str, Field(min_length=1)]
    side: Literal["buy", "sell"]
    quantity: Annotated[int, Field(gt=0)]
    price_yen: Annotated[Decimal, Field(gt=0, decimal_places=4)]
    executed_at: datetime

    @field_validator("executed_at", mode="before")
    @classmethod
    def _parse_time(cls, value: object) -> datetime:
        return _datetime(value)

    @field_validator("price_yen", mode="before")
    @classmethod
    def _parse_price(cls, value: object) -> Decimal:
        return _price(value)


class ExecutionLifecycleDocument(BaseModel):
    """Versioned contract for one ticker's human-approved execution lifecycle."""

    model_config = _MODEL_CONFIG

    schema_version: Literal[1]
    position_id: Annotated[str, Field(min_length=1)]
    ticker: Annotated[str, Field(pattern=_TICKER_PATTERN)]
    as_of: datetime
    decision_intents: tuple[DecisionIntent, ...]
    orders: tuple[BrokerOrder, ...]
    executions: tuple[BrokerExecution, ...]

    @field_validator("as_of", mode="before")
    @classmethod
    def _parse_as_of(cls, value: object) -> datetime:
        return _datetime(value)

    @field_validator("decision_intents", "orders", "executions", mode="before")
    @classmethod
    def _parse_sequences(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def _document_shape(self) -> ExecutionLifecycleDocument:
        if not self.decision_intents:
            raise ValueError("decision_intents must not be empty")
        _require_unique((item.intent_id for item in self.decision_intents), "intent_id")
        _require_unique((item.order_id for item in self.orders), "order_id")
        _require_unique((item.execution_id for item in self.executions), "execution_id")
        _require_unique(
            (
                item.external_broker_order_id
                for item in self.orders
                if item.external_broker_order_id is not None
            ),
            "external_broker_order_id",
        )
        _require_ordered(
            (item.confirmed_at for item in self.decision_intents),
            "decision_intents must be ordered",
        )
        _require_ordered((item.submitted_at for item in self.orders), "orders must be ordered")
        _require_ordered(
            (item.executed_at for item in self.executions), "executions must be ordered"
        )
        all_times = [
            self.as_of,
            *(item.confirmed_at for item in self.decision_intents),
            *(item.submitted_at for item in self.orders),
            *(item.terminal_at for item in self.orders if item.terminal_at is not None),
            *(item.executed_at for item in self.executions),
        ]
        if any(item > self.as_of for item in all_times[1:]):
            raise ValueError("lifecycle events cannot occur after as_of")
        return self


@dataclass(frozen=True, slots=True)
class IntentView:
    intent_id: str
    filled_quantity: int
    remaining_quantity: int


@dataclass(frozen=True, slots=True)
class OrderView:
    order_id: str
    origin_intent_id: str
    state: Literal[
        "submitted", "partially_filled", "filled", "broker_rejected", "cancelled", "expired"
    ]
    terminal_status: Literal["broker_rejected", "cancelled", "expired"] | None
    filled_quantity: int
    remaining_quantity: int


@dataclass(frozen=True, slots=True)
class ExecutionLifecycleView:
    position_id: str
    ticker: str
    position_state: Literal["none", "open", "closed"]
    current_quantity: int
    entry_date: datetime | None
    weighted_buy_price_yen: Decimal | None
    intents: tuple[IntentView, ...]
    orders: tuple[OrderView, ...]


def load_execution_lifecycle(path: Path) -> ExecutionLifecycleDocument:
    """Load a strict lifecycle YAML document."""

    try:
        raw = safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ExecutionLifecycleError(f"failed to read execution lifecycle: {error}") from error
    if not isinstance(raw, Mapping):
        raise ExecutionLifecycleError("execution lifecycle root must be a mapping")
    try:
        return ExecutionLifecycleDocument.model_validate(raw)
    except ValidationError as error:
        raise ExecutionLifecycleError(str(error)) from error


def execution_lifecycle_json_schema() -> dict[str, object]:
    """Return the public JSON Schema for the staged execution lifecycle contract."""

    schema = ExecutionLifecycleDocument.model_json_schema()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "execution-lifecycle",
        **schema,
    }


def evaluate_execution_lifecycle(
    document: ExecutionLifecycleDocument,
    *,
    board_lot: int,
) -> ExecutionLifecycleView:
    """Derive order and position state from confirmed facts without inventing fills."""

    if board_lot <= 0:
        raise ExecutionLifecycleError("board_lot must be positive")
    intents = {item.intent_id: item for item in document.decision_intents}
    orders = {item.order_id: item for item in document.orders}
    executions_by_order: dict[str, list[BrokerExecution]] = defaultdict(list)
    executions_by_intent: dict[str, list[BrokerExecution]] = defaultdict(list)

    for intent in document.decision_intents:
        _require_lot(intent.quantity, board_lot, f"intent {intent.intent_id} quantity")

    for order in document.orders:
        order_intent = intents.get(order.origin_intent_id)
        if order_intent is None:
            raise ExecutionLifecycleError(
                f"order {order.order_id} references unknown intent {order.origin_intent_id}"
            )
        _require_lot(order.submitted_quantity, board_lot, f"order {order.order_id} quantity")
        if order.submitted_at < order_intent.confirmed_at:
            raise ExecutionLifecycleError(
                f"order {order.order_id} predates its user-confirmed intent"
            )
        if order.submitted_at >= order_intent.expires_at:
            raise ExecutionLifecycleError(f"order {order.order_id} is submitted at or after expiry")
        if order_intent.side == "buy" and order.limit_price_yen > order_intent.price_guard_yen:
            raise ExecutionLifecycleError(f"buy order {order.order_id} exceeds intent price guard")
        if order_intent.side == "sell" and order.limit_price_yen < order_intent.price_guard_yen:
            raise ExecutionLifecycleError(
                f"sell order {order.order_id} is below intent price guard"
            )
        if (
            order.terminal_status == "expired"
            and order.terminal_at is not None
            and order.terminal_at < order_intent.expires_at
        ):
            raise ExecutionLifecycleError(f"expired order {order.order_id} predates intent expiry")
        if (
            order.terminal_status in {"broker_rejected", "cancelled"}
            and order.terminal_at is not None
            and order.terminal_at >= order_intent.expires_at
        ):
            raise ExecutionLifecycleError(
                f"order {order.order_id} must be marked expired at or after intent expiry"
            )

    for execution in document.executions:
        execution_order = orders.get(execution.order_id)
        if execution_order is None:
            raise ExecutionLifecycleError(
                f"execution {execution.execution_id} references unknown order {execution.order_id}"
            )
        intent = intents[execution_order.origin_intent_id]
        _require_lot(execution.quantity, board_lot, f"execution {execution.execution_id} quantity")
        if execution.side != intent.side:
            raise ExecutionLifecycleError(
                f"execution {execution.execution_id} side does not match order intent"
            )
        if execution.executed_at < execution_order.submitted_at:
            raise ExecutionLifecycleError(
                f"execution {execution.execution_id} predates order submission"
            )
        if execution.executed_at >= intent.expires_at:
            raise ExecutionLifecycleError(
                f"execution {execution.execution_id} occurs at or after expiry"
            )
        if (
            execution_order.terminal_at is not None
            and execution.executed_at > execution_order.terminal_at
        ):
            raise ExecutionLifecycleError(
                f"execution {execution.execution_id} follows order termination"
            )
        if execution.side == "buy" and execution.price_yen > execution_order.limit_price_yen:
            raise ExecutionLifecycleError(
                f"buy execution {execution.execution_id} exceeds price guard"
            )
        if execution.side == "sell" and execution.price_yen < execution_order.limit_price_yen:
            raise ExecutionLifecycleError(
                f"sell execution {execution.execution_id} is below price guard"
            )
        executions_by_order[execution_order.order_id].append(execution)
        executions_by_intent[intent.intent_id].append(execution)

    _validate_order_capacity(document, intents, executions_by_order)
    _validate_intent_capacity(document, executions_by_intent)
    _validate_open_orders_at_as_of(document, intents, executions_by_order)
    current_quantity = _validate_position_quantity(_execution_replay_order(document.executions))

    order_views = tuple(
        _order_view(order, executions_by_order[order.order_id]) for order in document.orders
    )
    intent_views = tuple(
        _intent_view(intent, executions_by_intent[intent.intent_id])
        for intent in document.decision_intents
    )
    buy_executions = tuple(item for item in document.executions if item.side == "buy")
    if not buy_executions:
        position_state: Literal["none", "open", "closed"] = "none"
        entry_date = None
        weighted_buy_price = None
    else:
        position_state = "open" if current_quantity > 0 else "closed"
        entry_date = buy_executions[0].executed_at
        bought_quantity = sum(item.quantity for item in buy_executions)
        bought_notional = sum(
            (item.price_yen * item.quantity for item in buy_executions), Decimal()
        )
        weighted_buy_price = bought_notional / bought_quantity
    return ExecutionLifecycleView(
        position_id=document.position_id,
        ticker=document.ticker,
        position_state=position_state,
        current_quantity=current_quantity,
        entry_date=entry_date,
        weighted_buy_price_yen=weighted_buy_price,
        intents=intent_views,
        orders=order_views,
    )


def reconcile_execution_lifecycle_with_ledger(
    document: ExecutionLifecycleDocument,
    ledger: PortfolioLedgerDocument,
    *,
    board_lot: int,
) -> ExecutionLifecycleView:
    """Verify that lifecycle facts and the canonical ledger describe the same execution."""

    view = evaluate_execution_lifecycle(document, board_lot=board_lot)
    try:
        reconcile_portfolio(ledger)
    except PortfolioLedgerError as error:
        raise ExecutionLifecycleError(f"ledger cannot be reconciled: {error}") from error

    if ledger.as_of != document.as_of:
        raise ExecutionLifecycleError("lifecycle and ledger as_of must match")

    intents = {item.intent_id: item for item in document.decision_intents}
    lifecycle_started_at = min(intent.confirmed_at for intent in document.decision_intents)
    reservations_by_order: dict[str, list[ReservationEvent]] = defaultdict(list)
    executions_by_id: dict[str, list[ExecutionEvent]] = defaultdict(list)
    releases_by_reservation: dict[str, list[ReleaseEvent]] = defaultdict(list)
    for event in ledger.events:
        match event:
            case ReservationEvent():
                reservations_by_order[event.order_id].append(event)
            case ExecutionEvent():
                executions_by_id[event.execution_id].append(event)
            case ReleaseEvent():
                releases_by_reservation[event.reservation_id].append(event)

    lifecycle_buy_order_ids = {
        order.order_id for order in document.orders if intents[order.origin_intent_id].side == "buy"
    }
    ledger_buy_order_ids = {
        reservation.order_id
        for reservations in reservations_by_order.values()
        for reservation in reservations
        if reservation.ticker == document.ticker and reservation.occurred_at >= lifecycle_started_at
    }
    if ledger_buy_order_ids != lifecycle_buy_order_ids:
        raise ExecutionLifecycleError(
            "same-ticker ledger reservations must equal lifecycle buy orders"
        )
    lifecycle_execution_ids = {execution.execution_id for execution in document.executions}
    ledger_execution_ids = {
        execution.execution_id
        for executions in executions_by_id.values()
        for execution in executions
        if execution.ticker == document.ticker and execution.occurred_at >= lifecycle_started_at
    }
    if ledger_execution_ids != lifecycle_execution_ids:
        raise ExecutionLifecycleError(
            "same-ticker ledger executions must equal lifecycle executions"
        )

    reservation_for_order: dict[str, ReservationEvent] = {}
    for order in document.orders:
        intent = intents[order.origin_intent_id]
        reservations = reservations_by_order[order.order_id]
        if intent.side == "sell":
            if reservations:
                raise ExecutionLifecycleError(f"sell order {order.order_id} must not reserve cash")
            continue
        if len(reservations) != 1:
            raise ExecutionLifecycleError(
                f"buy order {order.order_id} requires exactly one ledger reservation"
            )
        reservation = reservations[0]
        if (
            reservation.ticker != document.ticker
            or reservation.occurred_at != order.submitted_at
            or reservation.quantity != order.submitted_quantity
            or reservation.price_guard_yen != order.limit_price_yen
            or reservation.expires_at != intent.expires_at
        ):
            raise ExecutionLifecycleError(
                f"ledger reservation does not match buy order {order.order_id}"
            )
        reservation_for_order[order.order_id] = reservation

    for execution in document.executions:
        matches = executions_by_id[execution.execution_id]
        if len(matches) != 1:
            raise ExecutionLifecycleError(
                f"execution {execution.execution_id} requires exactly one ledger execution"
            )
        ledger_execution = matches[0]
        if (
            ledger_execution.ticker != document.ticker
            or ledger_execution.side != execution.side
            or ledger_execution.quantity != execution.quantity
            or ledger_execution.price_yen != execution.price_yen
            or ledger_execution.occurred_at != execution.executed_at
        ):
            raise ExecutionLifecycleError(
                f"ledger execution does not match {execution.execution_id}"
            )
        if execution.side == "buy":
            reservation = reservation_for_order[execution.order_id]
            if ledger_execution.reservation_id != reservation.reservation_id:
                raise ExecutionLifecycleError(
                    "buy execution "
                    f"{execution.execution_id} references the wrong ledger reservation"
                )
        elif ledger_execution.reservation_id is not None:
            raise ExecutionLifecycleError(
                f"sell execution {execution.execution_id} must not reference a ledger reservation"
            )

    order_views = {item.order_id: item for item in view.orders}
    for order in document.orders:
        if order.terminal_status is None:
            continue
        if intents[order.origin_intent_id].side == "sell":
            continue
        order_view = order_views[order.order_id]
        if order_view.remaining_quantity == 0:
            continue
        reservation = reservation_for_order[order.order_id]
        releases = releases_by_reservation[reservation.reservation_id]
        if not any(
            release.reason == order.terminal_status
            and order.terminal_at is not None
            and release.occurred_at >= order.terminal_at
            for release in releases
        ):
            raise ExecutionLifecycleError(
                f"terminal buy order {order.order_id} requires an explicit matching ledger release"
            )
    return view


def _require_unique(values: Iterable[str], label: str) -> None:
    materialized = tuple(values)
    if len(set(materialized)) != len(materialized):
        raise ValueError(f"{label} must be unique")


def _require_ordered(values: Iterable[datetime], message: str) -> None:
    materialized = tuple(values)
    if any(current > following for current, following in pairwise(materialized)):
        raise ValueError(message)


def _require_lot(quantity: int, board_lot: int, field: str) -> None:
    if quantity % board_lot:
        raise ExecutionLifecycleError(f"{field} must be a multiple of board_lot {board_lot}")


def _validate_order_capacity(
    document: ExecutionLifecycleDocument,
    intents: Mapping[str, DecisionIntent],
    executions_by_order: Mapping[str, Sequence[BrokerExecution]],
) -> None:
    orders_by_intent: dict[str, list[BrokerOrder]] = defaultdict(list)
    for order in document.orders:
        orders_by_intent[order.origin_intent_id].append(order)
        filled = sum(item.quantity for item in executions_by_order[order.order_id])
        if filled > order.submitted_quantity:
            raise ExecutionLifecycleError(
                f"order {order.order_id} execution quantity exceeds submission"
            )
        if filled == order.submitted_quantity and order.terminal_status is not None:
            raise ExecutionLifecycleError(
                f"fully filled order {order.order_id} must not carry a terminal status"
            )
        if order.terminal_status == "broker_rejected" and filled:
            raise ExecutionLifecycleError(
                f"broker-rejected order {order.order_id} must not contain executions"
            )

    for intent_id, orders in orders_by_intent.items():
        intent = intents[intent_id]
        prior: list[BrokerOrder] = []
        for order in orders:
            filled_before = sum(
                execution.quantity
                for earlier in prior
                for execution in executions_by_order[earlier.order_id]
                if execution.executed_at < order.submitted_at
            )
            outstanding = sum(
                earlier.submitted_quantity
                - sum(
                    execution.quantity
                    for execution in executions_by_order[earlier.order_id]
                    if execution.executed_at < order.submitted_at
                )
                for earlier in prior
                if earlier.terminal_at is None or earlier.terminal_at >= order.submitted_at
            )
            if filled_before + outstanding + order.submitted_quantity > intent.quantity:
                raise ExecutionLifecycleError(
                    f"order {order.order_id} exceeds remaining intent quantity "
                    "while prior orders are live"
                )
            prior.append(order)


def _validate_intent_capacity(
    document: ExecutionLifecycleDocument,
    executions_by_intent: Mapping[str, Sequence[BrokerExecution]],
) -> None:
    for intent in document.decision_intents:
        filled = sum(item.quantity for item in executions_by_intent[intent.intent_id])
        if filled > intent.quantity:
            raise ExecutionLifecycleError(
                f"intent {intent.intent_id} execution quantity exceeds intent"
            )


def _validate_open_orders_at_as_of(
    document: ExecutionLifecycleDocument,
    intents: Mapping[str, DecisionIntent],
    executions_by_order: Mapping[str, Sequence[BrokerExecution]],
) -> None:
    for order in document.orders:
        filled = sum(item.quantity for item in executions_by_order[order.order_id])
        if filled == order.submitted_quantity or order.terminal_status is not None:
            continue
        intent = intents[order.origin_intent_id]
        if document.as_of >= intent.expires_at:
            raise ExecutionLifecycleError(
                f"open order {order.order_id} has passed expiry without terminal status"
            )


def _execution_replay_order(executions: Sequence[BrokerExecution]) -> tuple[BrokerExecution, ...]:
    """Use broker timestamps as the replay order and reject ambiguous mixed-side ties."""

    grouped_sides: dict[datetime, set[str]] = defaultdict(set)
    for execution in executions:
        grouped_sides[execution.executed_at].add(execution.side)
    if any(len(sides) > 1 for sides in grouped_sides.values()):
        raise ExecutionLifecycleError(
            "buy and sell executions cannot share the same broker timestamp"
        )
    return tuple(sorted(executions, key=lambda execution: execution.executed_at))


def _validate_position_quantity(executions: Sequence[BrokerExecution]) -> int:
    quantity = 0
    position_was_closed = False
    for execution in executions:
        if execution.side == "buy":
            if position_was_closed:
                raise ExecutionLifecycleError(
                    "a lifecycle position cannot buy again after it has closed"
                )
            quantity += execution.quantity
        else:
            if execution.quantity > quantity:
                raise ExecutionLifecycleError(
                    f"sell execution {execution.execution_id} exceeds current repository holding"
                )
            quantity -= execution.quantity
            if quantity == 0:
                position_was_closed = True
    return quantity


def _order_view(order: BrokerOrder, executions: Sequence[BrokerExecution]) -> OrderView:
    filled = sum(item.quantity for item in executions)
    remaining = order.submitted_quantity - filled
    if remaining == 0:
        state: Literal[
            "submitted", "partially_filled", "filled", "broker_rejected", "cancelled", "expired"
        ] = "filled"
    elif filled > 0:
        state = "partially_filled"
    elif order.terminal_status is not None:
        state = order.terminal_status
    else:
        state = "submitted"
    return OrderView(
        order_id=order.order_id,
        origin_intent_id=order.origin_intent_id,
        state=state,
        terminal_status=order.terminal_status,
        filled_quantity=filled,
        remaining_quantity=remaining,
    )


def _intent_view(intent: DecisionIntent, executions: Sequence[BrokerExecution]) -> IntentView:
    filled = sum(item.quantity for item in executions)
    return IntentView(
        intent_id=intent.intent_id,
        filled_quantity=filled,
        remaining_quantity=intent.quantity - filled,
    )
