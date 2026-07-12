from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from baibai_loop.foundation.yaml_io import safe_load
from baibai_loop.position.execution import (
    ExecutionLifecycleDocument,
    ExecutionLifecycleError,
    evaluate_execution_lifecycle,
    execution_lifecycle_json_schema,
    load_execution_lifecycle,
    reconcile_execution_lifecycle_with_ledger,
)
from baibai_loop.position.ledger import PortfolioLedgerDocument
from baibai_loop.validation.execution_lifecycle import validate_execution_lifecycle_file

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests/fixtures/execution-lifecycle/representative.yaml"
LEDGER_FIXTURE = ROOT / "tests/fixtures/execution-lifecycle/ledger.yaml"
SCHEMA = ROOT / "records/_schemas/execution-lifecycle.json"


def _raw(path: Path = FIXTURE) -> dict[str, object]:
    raw = safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return copy.deepcopy(raw)


def _document(raw: dict[str, object] | None = None) -> ExecutionLifecycleDocument:
    return ExecutionLifecycleDocument.model_validate(raw or _raw())


def _write(path: Path, raw: dict[str, object]) -> Path:
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def test_generated_public_schema_matches_tracked_contract() -> None:
    assert json.loads(SCHEMA.read_text(encoding="utf-8")) == execution_lifecycle_json_schema()


def test_representative_fixture_passes_staged_validator() -> None:
    assert validate_execution_lifecycle_file(FIXTURE) == []


def test_representative_lifecycle_derives_retry_partial_add_and_full_exit() -> None:
    view = evaluate_execution_lifecycle(load_execution_lifecycle(FIXTURE), board_lot=100)

    assert view.position_state == "closed"
    assert view.current_quantity == 0
    assert view.entry_date is not None
    assert view.entry_date.isoformat() == "2026-06-03T10:00:00+09:00"
    assert view.weighted_buy_price_yen == 1000
    assert [
        (item.intent_id, item.filled_quantity, item.remaining_quantity) for item in view.intents
    ] == [
        ("intent-2331-initial", 100, 100),
        ("intent-2331-reprice", 100, 0),
        ("intent-2331-add", 100, 0),
        ("intent-2331-exit", 300, 0),
    ]
    assert [(item.order_id, item.state, item.terminal_status) for item in view.orders] == [
        ("order-2331-initial", "partially_filled", "cancelled"),
        ("order-2331-initial-retry", "expired", "expired"),
        ("order-2331-reprice", "filled", None),
        ("order-2331-add", "filled", None),
        ("order-2331-exit", "filled", None),
    ]


def test_representative_lifecycle_matches_reservations_and_executions_in_ledger() -> None:
    ledger = PortfolioLedgerDocument.model_validate(_raw(LEDGER_FIXTURE))

    view = reconcile_execution_lifecycle_with_ledger(
        load_execution_lifecycle(FIXTURE), ledger, board_lot=100
    )

    assert view.position_state == "closed"
    assert view.current_quantity == 0


def test_same_intent_can_retry_after_a_terminal_order_without_new_confirmation() -> None:
    view = evaluate_execution_lifecycle(_document(), board_lot=100)

    retry = next(item for item in view.orders if item.order_id == "order-2331-initial-retry")
    assert retry.state == "expired"
    assert retry.remaining_quantity == 100


def test_concurrent_retry_cannot_exceed_the_remaining_intent_quantity() -> None:
    raw = _raw()
    orders = raw["orders"]
    assert isinstance(orders, list)
    orders.insert(
        1,
        {
            "order_id": "order-2331-overlap",
            "origin_intent_id": "intent-2331-initial",
            "external_broker_order_id": None,
            "submitted_at": "2026-06-03T09:30:00+09:00",
            "submitted_quantity": 200,
            "limit_price_yen": 1040,
            "terminal_status": "cancelled",
            "terminal_at": "2026-06-03T11:00:00+09:00",
        },
    )

    with pytest.raises(ExecutionLifecycleError, match="exceeds remaining intent quantity"):
        evaluate_execution_lifecycle(_document(raw), board_lot=100)


def _mutate_orphan_order(raw: dict[str, object]) -> None:
    orders = raw["orders"]
    assert isinstance(orders, list)
    order = orders[0]
    assert isinstance(order, dict)
    order["origin_intent_id"] = "missing-intent"


def _mutate_side_mismatch(raw: dict[str, object]) -> None:
    executions = raw["executions"]
    assert isinstance(executions, list)
    execution = executions[0]
    assert isinstance(execution, dict)
    execution["side"] = "sell"


def _mutate_guard_violation(raw: dict[str, object]) -> None:
    executions = raw["executions"]
    assert isinstance(executions, list)
    execution = executions[0]
    assert isinstance(execution, dict)
    execution["price_yen"] = 1051


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_mutate_orphan_order, "unknown intent"),
        (_mutate_side_mismatch, "side does not match"),
        (_mutate_guard_violation, "exceeds price guard"),
    ],
)
def test_evaluator_rejects_orphan_side_and_guard(
    mutate: Callable[[dict[str, object]], None], message: str
) -> None:
    raw = _raw()
    assert callable(mutate)
    mutate(raw)

    with pytest.raises(ExecutionLifecycleError, match=message):
        evaluate_execution_lifecycle(_document(raw), board_lot=100)


def test_evaluator_rejects_over_sell_after_matching_order_and_intent_quantities() -> None:
    raw = _raw()
    intents = raw["decision_intents"]
    orders = raw["orders"]
    executions = raw["executions"]
    assert isinstance(intents, list)
    assert isinstance(orders, list)
    assert isinstance(executions, list)
    exit_intent = intents[-1]
    exit_order = orders[-1]
    exit_execution = executions[-1]
    assert isinstance(exit_intent, dict)
    assert isinstance(exit_order, dict)
    assert isinstance(exit_execution, dict)
    exit_intent["quantity"] = 400
    exit_order["submitted_quantity"] = 400
    exit_execution["quantity"] = 400

    with pytest.raises(ExecutionLifecycleError, match="exceeds current repository holding"):
        evaluate_execution_lifecycle(_document(raw), board_lot=100)


def test_validator_rejects_unknown_fields_and_missing_user_decision_reference(
    tmp_path: Path,
) -> None:
    raw = _raw()
    intents = raw["decision_intents"]
    assert isinstance(intents, list)
    first = intents[0]
    assert isinstance(first, dict)
    first["decision_reference"] = ""
    first["unexpected"] = "must not be accepted"
    path = _write(tmp_path / "invalid.yaml", raw)

    findings = validate_execution_lifecycle_file(path)

    codes = {finding.code for finding in findings}
    assert "execution-lifecycle.additionalProperties" in codes
    assert "execution-lifecycle.minLength" in codes


def test_external_broker_order_id_cannot_be_reused() -> None:
    raw = _raw()
    orders = raw["orders"]
    assert isinstance(orders, list)
    first = orders[0]
    second = orders[1]
    assert isinstance(first, dict)
    assert isinstance(second, dict)
    first["external_broker_order_id"] = "broker-2331-1"
    second["external_broker_order_id"] = "broker-2331-1"

    with pytest.raises(ValueError, match="external_broker_order_id must be unique"):
        _document(raw)


@pytest.mark.parametrize(
    ("collection", "identifier"),
    [
        ("decision_intents", "intent_id"),
        ("orders", "order_id"),
        ("executions", "execution_id"),
    ],
)
def test_primary_lifecycle_ids_cannot_be_reused(collection: str, identifier: str) -> None:
    raw = _raw()
    items = raw[collection]
    assert isinstance(items, list)
    first = items[0]
    assert isinstance(first, dict)
    items.append(copy.deepcopy(first))

    with pytest.raises(ValueError, match=f"{identifier} must be unique"):
        _document(raw)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("quantity", True, "Input should be a valid integer"),
        ("price_guard_yen", "NaN", "must use fixed-point decimal notation"),
        ("confirmed_at", "2026-06-02T08:45:00", "must include a timezone"),
        ("uses_margin", True, "Input should be False"),
    ],
)
def test_intent_rejects_unsafe_quantity_price_time_and_margin(
    field: str, value: object, message: str
) -> None:
    raw = _raw()
    intents = raw["decision_intents"]
    assert isinstance(intents, list)
    first = intents[0]
    assert isinstance(first, dict)
    first[field] = value

    with pytest.raises(ValueError, match=message):
        _document(raw)


def test_open_order_past_expiry_requires_an_explicit_terminal_status() -> None:
    raw = _raw()
    executions = raw["executions"]
    assert isinstance(executions, list)
    executions[:] = [
        execution
        for execution in executions
        if not isinstance(execution, dict)
        or execution.get("order_id") not in {"order-2331-add", "order-2331-exit"}
    ]

    with pytest.raises(ExecutionLifecycleError, match="has passed expiry"):
        evaluate_execution_lifecycle(_document(raw), board_lot=100)


def test_fully_filled_order_cannot_also_be_cancelled_or_expired() -> None:
    raw = _raw()
    orders = raw["orders"]
    assert isinstance(orders, list)
    full_order = orders[2]
    assert isinstance(full_order, dict)
    full_order["terminal_status"] = "cancelled"
    full_order["terminal_at"] = "2026-07-01T11:00:00+09:00"

    with pytest.raises(ExecutionLifecycleError, match="fully filled order"):
        evaluate_execution_lifecycle(_document(raw), board_lot=100)


def test_terminal_order_after_expiry_must_be_marked_expired() -> None:
    raw = _raw()
    orders = raw["orders"]
    assert isinstance(orders, list)
    partial_order = orders[1]
    assert isinstance(partial_order, dict)
    partial_order["terminal_status"] = "cancelled"

    with pytest.raises(ExecutionLifecycleError, match="must be marked expired"):
        evaluate_execution_lifecycle(_document(raw), board_lot=100)


def test_broker_rejected_order_cannot_contain_a_partial_execution() -> None:
    raw = _raw()
    orders = raw["orders"]
    assert isinstance(orders, list)
    initial = orders[0]
    assert isinstance(initial, dict)
    initial["terminal_status"] = "broker_rejected"

    with pytest.raises(ExecutionLifecycleError, match="must not contain executions"):
        evaluate_execution_lifecycle(_document(raw), board_lot=100)


def test_execution_lifecycle_requires_explicit_nullable_broker_evidence() -> None:
    raw = _raw()
    orders = raw["orders"]
    assert isinstance(orders, list)
    initial = orders[0]
    assert isinstance(initial, dict)
    del initial["external_broker_order_id"]

    with pytest.raises(ValueError, match="Field required"):
        _document(raw)


def test_same_broker_timestamp_cannot_mix_buy_and_sell_executions() -> None:
    raw = _raw()
    intents = raw["decision_intents"]
    orders = raw["orders"]
    executions = raw["executions"]
    assert isinstance(intents, list)
    assert isinstance(orders, list)
    assert isinstance(executions, list)
    exit_intent = intents[-1]
    exit_order = orders[-1]
    exit_execution = executions[-1]
    assert isinstance(exit_intent, dict)
    assert isinstance(exit_order, dict)
    assert isinstance(exit_execution, dict)
    exit_intent["confirmed_at"] = "2026-07-03T08:45:00+09:00"
    exit_intent["expires_at"] = "2026-07-03T15:30:00+09:00"
    exit_order["submitted_at"] = "2026-07-03T09:30:00+09:00"
    exit_execution["executed_at"] = "2026-07-03T10:00:00+09:00"

    with pytest.raises(ExecutionLifecycleError, match="cannot share the same broker timestamp"):
        evaluate_execution_lifecycle(_document(raw), board_lot=100)


def test_lifecycle_position_cannot_reenter_after_a_full_exit() -> None:
    raw = _raw()
    intents = raw["decision_intents"]
    orders = raw["orders"]
    executions = raw["executions"]
    assert isinstance(intents, list)
    assert isinstance(orders, list)
    assert isinstance(executions, list)
    intents.append(
        {
            "intent_id": "intent-2331-reentry",
            "decision_reference": "user-decision-2331-reentry",
            "decision_packet_sha256": "e" * 64,
            "confirmed_at": "2026-07-10T11:00:00+09:00",
            "side": "buy",
            "quantity": 100,
            "price_guard_yen": 1000,
            "expires_at": "2026-07-10T15:30:00+09:00",
            "uses_margin": False,
        }
    )
    orders.append(
        {
            "order_id": "order-2331-reentry",
            "origin_intent_id": "intent-2331-reentry",
            "external_broker_order_id": None,
            "submitted_at": "2026-07-10T11:01:00+09:00",
            "submitted_quantity": 100,
            "limit_price_yen": 1000,
            "terminal_status": None,
            "terminal_at": None,
        }
    )
    executions.append(
        {
            "execution_id": "execution-2331-reentry",
            "order_id": "order-2331-reentry",
            "side": "buy",
            "quantity": 100,
            "price_yen": 990,
            "executed_at": "2026-07-10T12:00:00+09:00",
        }
    )

    with pytest.raises(ExecutionLifecycleError, match="cannot buy again after it has closed"):
        evaluate_execution_lifecycle(_document(raw), board_lot=100)


def test_terminal_order_requires_matching_ledger_release() -> None:
    raw = _raw(LEDGER_FIXTURE)
    events = raw["events"]
    assert isinstance(events, list)
    events[:] = [
        event
        for event in events
        if not isinstance(event, dict)
        or event.get("event_id") != "release-2331-initial-retry-expired"
    ]

    with pytest.raises(
        ExecutionLifecycleError, match="expired reservations require an explicit release"
    ):
        reconcile_execution_lifecycle_with_ledger(
            load_execution_lifecycle(FIXTURE),
            PortfolioLedgerDocument.model_validate(raw),
            board_lot=100,
        )


def test_ledger_reconciliation_requires_the_same_snapshot_time() -> None:
    raw = _raw(LEDGER_FIXTURE)
    raw["as_of"] = "2026-07-10T16:00:00+09:00"

    with pytest.raises(ExecutionLifecycleError, match="as_of must match"):
        reconcile_execution_lifecycle_with_ledger(
            load_execution_lifecycle(FIXTURE),
            PortfolioLedgerDocument.model_validate(raw),
            board_lot=100,
        )


def test_ledger_reconciliation_rejects_extra_same_ticker_reservation() -> None:
    raw = _raw(LEDGER_FIXTURE)
    events = raw["events"]
    assert isinstance(events, list)
    events.append(
        {
            "event_id": "reserve-2331-unrecorded",
            "type": "reservation",
            "occurred_at": "2026-07-10T11:00:00+09:00",
            "reservation_id": "reservation-2331-unrecorded",
            "order_id": "order-2331-unrecorded",
            "ticker": "2331",
            "sector": "サービス業",
            "common_factors": ["labor-automation"],
            "quantity": 100,
            "price_guard_yen": 1000,
            "expires_at": "2026-07-15T15:30:00+09:00",
        }
    )

    with pytest.raises(ExecutionLifecycleError, match="reservations must equal"):
        reconcile_execution_lifecycle_with_ledger(
            load_execution_lifecycle(FIXTURE),
            PortfolioLedgerDocument.model_validate(raw),
            board_lot=100,
        )


def test_ledger_reconciliation_excludes_a_closed_round_before_lifecycle_start() -> None:
    raw = _raw(LEDGER_FIXTURE)
    events = raw["events"]
    assert isinstance(events, list)
    opening = events[0]
    assert isinstance(opening, dict)
    opening["occurred_at"] = "2026-05-01T08:00:00+09:00"
    events[1:1] = [
        {
            "event_id": "reserve-2331-old-round",
            "type": "reservation",
            "occurred_at": "2026-05-01T09:00:00+09:00",
            "reservation_id": "reservation-2331-old-round",
            "order_id": "order-2331-old-round",
            "ticker": "2331",
            "sector": "サービス業",
            "common_factors": ["labor-automation"],
            "quantity": 100,
            "price_guard_yen": 1000,
            "expires_at": "2026-05-01T15:30:00+09:00",
        },
        {
            "event_id": "fill-2331-old-round",
            "type": "execution",
            "occurred_at": "2026-05-01T10:00:00+09:00",
            "execution_id": "execution-2331-old-round-buy",
            "reservation_id": "reservation-2331-old-round",
            "ticker": "2331",
            "side": "buy",
            "quantity": 100,
            "price_yen": 990,
        },
        {
            "event_id": "sell-2331-old-round",
            "type": "execution",
            "occurred_at": "2026-05-01T11:00:00+09:00",
            "execution_id": "execution-2331-old-round-sell",
            "reservation_id": None,
            "ticker": "2331",
            "side": "sell",
            "quantity": 100,
            "price_yen": 1000,
        },
    ]

    view = reconcile_execution_lifecycle_with_ledger(
        load_execution_lifecycle(FIXTURE),
        PortfolioLedgerDocument.model_validate(raw),
        board_lot=100,
    )

    assert view.position_state == "closed"


def test_ledger_reconciliation_allows_a_partially_filled_terminal_sell() -> None:
    lifecycle_raw = _raw()
    intents = lifecycle_raw["decision_intents"]
    orders = lifecycle_raw["orders"]
    executions = lifecycle_raw["executions"]
    assert isinstance(intents, list)
    assert isinstance(orders, list)
    assert isinstance(executions, list)
    exit_intent = intents[-1]
    exit_order = orders[-1]
    exit_execution = executions[-1]
    assert isinstance(exit_intent, dict)
    assert isinstance(exit_order, dict)
    assert isinstance(exit_execution, dict)
    exit_intent["quantity"] = 200
    exit_order["submitted_quantity"] = 200
    exit_order["terminal_status"] = "cancelled"
    exit_order["terminal_at"] = "2026-07-10T10:30:00+09:00"
    exit_execution["quantity"] = 100

    ledger_raw = _raw(LEDGER_FIXTURE)
    events = ledger_raw["events"]
    market_prices = ledger_raw["market_prices"]
    assert isinstance(events, list)
    assert isinstance(market_prices, list)
    exit_event = next(
        event
        for event in events
        if isinstance(event, dict) and event.get("execution_id") == "execution-2331-exit"
    )
    assert isinstance(exit_event, dict)
    exit_event["quantity"] = 100
    market_prices.append(
        {
            "ticker": "2331",
            "price_yen": 1000,
            "observed_at": "2026-07-10T11:00:00+09:00",
            "source_kind": "test_fixture",
            "price_basis": "current",
            "source_ref": "test:2331",
        }
    )

    view = reconcile_execution_lifecycle_with_ledger(
        _document(lifecycle_raw),
        PortfolioLedgerDocument.model_validate(ledger_raw),
        board_lot=100,
    )

    assert view.position_state == "open"
    assert view.current_quantity == 200


@pytest.mark.property
@given(lots=st.integers(min_value=1, max_value=20), filled_lots=st.integers(0, 20))
def test_position_state_is_derived_from_any_legal_partial_fill(lots: int, filled_lots: int) -> None:
    filled_lots = min(lots, filled_lots)
    quantity = lots * 100
    filled = filled_lots * 100
    terminal_status = None if filled == quantity else "cancelled"
    terminal_at = None if terminal_status is None else "2026-07-01T11:00:00+09:00"
    raw: dict[str, object] = {
        "schema_version": 1,
        "position_id": "position-1111",
        "ticker": "1111",
        "as_of": "2026-07-01T12:00:00+09:00",
        "decision_intents": [
            {
                "intent_id": "intent-1111-buy",
                "decision_reference": "user-decision-1111",
                "decision_packet_sha256": "d" * 64,
                "confirmed_at": "2026-07-01T08:30:00+09:00",
                "side": "buy",
                "quantity": quantity,
                "price_guard_yen": 1000,
                "expires_at": "2026-07-01T15:30:00+09:00",
                "uses_margin": False,
            }
        ],
        "orders": [
            {
                "order_id": "order-1111-buy",
                "origin_intent_id": "intent-1111-buy",
                "external_broker_order_id": None,
                "submitted_at": "2026-07-01T09:00:00+09:00",
                "submitted_quantity": quantity,
                "limit_price_yen": 1000,
                "terminal_status": terminal_status,
                "terminal_at": terminal_at,
            }
        ],
        "executions": (
            [
                {
                    "execution_id": "execution-1111-buy",
                    "order_id": "order-1111-buy",
                    "side": "buy",
                    "quantity": filled,
                    "price_yen": 1000,
                    "executed_at": "2026-07-01T10:00:00+09:00",
                }
            ]
            if filled
            else []
        ),
    }

    view = evaluate_execution_lifecycle(_document(raw), board_lot=100)

    assert view.current_quantity == filled
    assert view.position_state == ("open" if filled else "none")
