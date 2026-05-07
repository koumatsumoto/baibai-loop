"""Validate execution records in records/06-trades."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from .domain import (
    as_list,
    as_mapping,
    load_markdown_front_matter,
    load_snapshot_mapping,
    number,
    repo_root_for,
    resolve_ref,
)
from .errors import ValidationFinding
from .registry import evaluate_kill_switch, has_validator_callable

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "records" / "_schemas" / "trade.json"

_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
_TICKER_PATTERN = re.compile(r"^[0-9A-Z]{4}$")
_REMOVED_FRONT_MATTER_FIELDS = {
    "status",
    "_".join(("order", "date")),
    "_".join(("entry", "date")),
    "_".join(("entry", "price")),
    "_".join(("exit", "date")),
    "_".join(("exit", "price")),
    "_".join(("pnl", "pct")),
    "_".join(("real", "order", "notional", "yen")),
    "_".join(("tactical", "capital", "yen")),
    "_".join(("tactical", "concentration", "pct")),
}
_ORDER_STATES = {
    "submitted",
    "broker_rejected",
    "cancelled",
    "expired",
    "not_filled",
    "partially_filled",
    "filled",
}


def _load_validator() -> Draft202012Validator:
    raw = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"unexpected schema root: {SCHEMA_PATH}")
    Draft202012Validator.check_schema(raw)
    return Draft202012Validator(raw)


_VALIDATOR = _load_validator()


def discover_trade_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.rglob("*.md")
        if path.is_file() and not path.name.startswith("template")
    )


def validate_trade_file(path: Path) -> list[ValidationFinding]:
    loaded = _load_front_matter(path)
    if isinstance(loaded, list):
        return loaded
    front = loaded
    findings: list[ValidationFinding] = []
    findings.extend(_validate_schema(path, front))
    findings.extend(_check_removed_fields(path, front))
    findings.extend(_check_policy_and_calendar_context(path, front))
    findings.extend(_check_ticker(path, front))
    findings.extend(_check_order_join(path, front))
    findings.extend(_check_decision_register_intent_join(path, front))
    findings.extend(_check_order_state_consistency(path, front))
    findings.extend(_check_current_quantity(path, front))
    findings.extend(_check_guarded_notional(path, front))
    findings.extend(_check_intent_recomputed(path, front))
    findings.extend(_check_kill_switches(path, front))
    findings.extend(_check_entry_legs(path, front))
    return findings


def _load_front_matter(path: Path) -> dict[str, object] | list[ValidationFinding]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.io",
                message=f"failed to read file: {exc}",
            )
        ]
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.no-front-matter",
                message="trade markdown must start with YAML front matter",
            )
        ]
    try:
        front = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.invalid-yaml",
                message=f"front matter YAML parse failed: {exc}",
            )
        ]
    if not isinstance(front, dict):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.front-matter-non-mapping",
                message="trade front matter must be a mapping",
            )
        ]
    return front


def _validate_schema(path: Path, front: Mapping[str, object]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for error in _VALIDATOR.iter_errors(front):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=f"trade.{error.validator or 'invalid'}",
                message=str(error.message),
                location=_format_path(error.absolute_path),
            )
        )
    return findings


def _check_removed_fields(path: Path, front: Mapping[str, object]) -> list[ValidationFinding]:
    return [
        ValidationFinding(
            severity="error",
            target=path,
            code="trade.removed-field",
            message=f"removed front matter field is not allowed: {field}",
            location=field,
        )
        for field in sorted(_REMOVED_FRONT_MATTER_FIELDS)
        if field in front
    ]


def _check_policy_and_calendar_context(
    path: Path, front: Mapping[str, object]
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    if front.get("policy_applicability") != "active":
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.policy-applicability",
                message="trade records require policy_applicability: active",
                location="policy_applicability",
            )
        )
    findings.extend(_check_calendar_snapshot_block(path, front.get("calendars_snapshot")))
    return findings


def _check_calendar_snapshot_block(path: Path, value: object) -> list[ValidationFinding]:
    if not isinstance(value, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.calendars-snapshot",
                message="calendars_snapshot must pin business_days, events, and corporate_actions",
                location="calendars_snapshot",
            )
        ]
    findings: list[ValidationFinding] = []
    for key in ("business_days", "events", "corporate_actions"):
        item = value.get(key)
        location = f"calendars_snapshot.{key}"
        if not isinstance(item, Mapping):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="trade.calendars-snapshot",
                    message=f"{location} must be a snapshot ref",
                    location=location,
                )
            )
            continue
        ref_path = item.get("ref_path")
        digest = item.get("content_sha256")
        if not isinstance(ref_path, str) or not ref_path:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="trade.calendars-snapshot-ref",
                    message=f"{location}.ref_path is required",
                    location=f"{location}.ref_path",
                )
            )
        if not isinstance(digest, str) or not re.match(r"^sha256:[0-9a-f]{64}$", digest):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="trade.calendars-snapshot-hash",
                    message=f"{location}.content_sha256 must be sha256:<64 lowercase hex>",
                    location=f"{location}.content_sha256",
                )
            )
    return findings


def _check_ticker(path: Path, front: Mapping[str, object]) -> list[ValidationFinding]:
    ticker = front.get("ticker")
    if not isinstance(ticker, str) or not _TICKER_PATTERN.match(ticker):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.ticker-format",
                message=f"ticker must be 4 alphanumeric uppercase chars (got {ticker!r})",
                location="ticker",
            )
        ]
    if not path.name.endswith(f"-{ticker}.md"):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.filename-ticker",
                message="trade filename must end with -<ticker>.md",
                location="ticker",
            )
        ]
    return []


def _check_order_join(path: Path, front: Mapping[str, object]) -> list[ValidationFinding]:
    intent = front.get("order_intent")
    orders = front.get("orders")
    if not isinstance(intent, Mapping) or not isinstance(orders, list):
        return []
    intent_id = intent.get("order_intent_id")
    if not isinstance(intent_id, str):
        return []
    if not any(
        isinstance(order, Mapping) and order.get("origin_order_intent_id") == intent_id
        for order in orders
    ):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.order-intent-join",
                message="orders[].origin_order_intent_id must join to order_intent.order_intent_id",
                location="orders",
            )
        ]
    return []


def _check_decision_register_intent_join(
    path: Path, front: Mapping[str, object]
) -> list[ValidationFinding]:
    if not _is_repository_trade_record(path):
        return []
    intent = as_mapping(front.get("order_intent"))
    decision_event_id = intent.get("decision_event_id")
    intent_id = intent.get("order_intent_id")
    if not isinstance(decision_event_id, str) or not isinstance(intent_id, str):
        return []
    root = repo_root_for(path)
    ledger_root = root / "records/_ledger/research-decisions"
    if not ledger_root.is_dir():
        return []
    for register_path in sorted(ledger_root.glob("*.jsonl")):
        for line in register_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, Mapping) or row.get("decision_event_id") != decision_event_id:
                continue
            register_intent = as_mapping(row.get("order_intent"))
            if register_intent.get("order_intent_id") != intent_id:
                return [
                    ValidationFinding(
                        severity="error",
                        target=path,
                        code="trade.decision-register-intent-join",
                        message=(
                            "order_intent.order_intent_id must match decision register "
                            "order_intent for the same decision_event_id"
                        ),
                        location="order_intent.order_intent_id",
                    )
                ]
            return []
    return [
        ValidationFinding(
            severity="error",
            target=path,
            code="trade.decision-register-intent-missing",
            message="trade order_intent.decision_event_id must exist in decision register",
            location="order_intent.decision_event_id",
        )
    ]


def _check_order_state_consistency(
    path: Path, front: Mapping[str, object]
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    orders = front.get("orders")
    if not isinstance(orders, list):
        return findings
    for index, order in enumerate(orders):
        if not isinstance(order, Mapping):
            continue
        state = order.get("state")
        if state not in _ORDER_STATES:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="trade.order-state",
                    message=f"orders[{index}].state must be one of {_ORDER_STATES}",
                    location=f"orders[{index}].state",
                )
            )
        submitted = _number(order.get("submitted_quantity"))
        filled = _number(order.get("filled_quantity"))
        if submitted is not None and filled is not None and filled > submitted:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="trade.filled-quantity",
                    message="filled_quantity cannot exceed submitted_quantity",
                    location=f"orders[{index}].filled_quantity",
                )
            )
    position_state = front.get("position_state")
    executions = front.get("executions")
    has_executions = isinstance(executions, list) and bool(executions)
    if position_state == "none" and has_executions:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.position-execution-state",
                message="position_state: none cannot have executions",
                location="position_state",
            )
        )
    return findings


def _check_current_quantity(path: Path, front: Mapping[str, object]) -> list[ValidationFinding]:
    if "current_quantity" not in front and front.get("position_state") == "none":
        return []
    expected = 0.0
    for execution in as_list(front.get("executions")):
        if not isinstance(execution, Mapping):
            continue
        quantity = _number(execution.get("quantity")) or 0.0
        side = execution.get("side")
        if side == "buy":
            expected += quantity
        elif side == "sell":
            expected -= quantity
    for event in as_list(front.get("corporate_action_events")):
        if isinstance(event, Mapping):
            expected += _number(event.get("delta_quantity")) or 0.0
    actual = _number(front.get("current_quantity"))
    if actual is None:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.current-quantity-required",
                message="trade records with a position lifecycle require current_quantity",
                location="current_quantity",
            )
        ]
    if abs(actual - expected) > 1e-6:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.current-quantity",
                message=(
                    f"current_quantity must equal executions plus corporate action deltas "
                    f"({expected:g})"
                ),
                location="current_quantity",
            )
        ]
    return []


def _check_guarded_notional(path: Path, front: Mapping[str, object]) -> list[ValidationFinding]:
    intent = front.get("order_intent")
    sizing = front.get("position_sizing_overlay")
    if not isinstance(intent, Mapping) or not isinstance(sizing, Mapping):
        return []
    quantity = _number(intent.get("quantity"))
    guard = _number(intent.get("order_price_guard_yen"))
    guarded = _number(sizing.get("guarded_max_notional_yen"))
    if quantity is None or guard is None or guarded is None:
        return []
    expected = quantity * guard
    if abs(guarded - expected) > 1:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.guarded-notional",
                message=(
                    f"guarded_max_notional_yen must equal quantity * guard price ({expected:g})"
                ),
                location="position_sizing_overlay.guarded_max_notional_yen",
            )
        ]
    return []


def _check_intent_recomputed(path: Path, front: Mapping[str, object]) -> list[ValidationFinding]:
    intent = as_mapping(front.get("order_intent"))
    sizing = as_mapping(front.get("position_sizing_overlay"))
    if not intent or not sizing:
        return []
    expected = _derive_trade_order(path, front)
    findings: list[ValidationFinding] = []
    if expected is None:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.intent-source",
                message="submitted trade order intent must derive from a valid research_ref",
                location="research_ref",
            )
        )
        return findings
    for field, expected_value in (
        ("quantity", expected["quantity"]),
        ("order_price_guard_yen", expected["order_price_guard_yen"]),
        ("guarded_max_notional_yen", expected["guarded_notional_yen"]),
    ):
        source = intent if field in {"quantity", "order_price_guard_yen"} else sizing
        location = (
            f"order_intent.{field}" if source is intent else f"position_sizing_overlay.{field}"
        )
        actual = _number(source.get(field))
        if actual is None or abs(actual - expected_value) > 1:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="trade.intent-derived",
                    message=(
                        f"{location} must derive to {expected_value:g} from "
                        "research, policy, and board lot"
                    ),
                    location=location,
                )
            )
    return findings


def _derive_trade_order(path: Path, front: Mapping[str, object]) -> dict[str, float] | None:
    root = repo_root_for(path)
    research_ref = front.get("research_ref")
    if not isinstance(research_ref, str):
        return None
    research_path = resolve_ref(root, research_ref)
    if not research_path.is_file():
        return None
    try:
        research = load_markdown_front_matter(research_path)
    except (OSError, ValueError, yaml.YAMLError):
        return None
    policy = _load_policy(path, front)
    order_constraints = as_mapping(policy.get("order_constraints"))
    board_lot = int(number(order_constraints.get("board_lot")) or 100)
    payoff = as_mapping(research.get("thesis_payoff"))
    guard = _number(payoff.get("max_entry_price_yen"))
    research_sizing = as_mapping(research.get("position_sizing_overlay"))
    real_intent = _number(research_sizing.get("real_order_intent_yen"))
    if guard is None or guard <= 0 or real_intent is None:
        return None
    quantity = math.floor(real_intent / guard / board_lot) * board_lot if board_lot else 0
    return {
        "quantity": float(quantity),
        "order_price_guard_yen": float(guard),
        "guarded_notional_yen": float(quantity * guard),
    }


def _load_policy(path: Path, front: Mapping[str, object]) -> Mapping[str, Any]:
    try:
        _policy_path, payload = load_snapshot_mapping(
            repo_root_for(path), front.get("policy_snapshot")
        )
        return payload
    except (OSError, ValueError, yaml.YAMLError):
        return {}


def _check_entry_legs(path: Path, front: Mapping[str, object]) -> list[ValidationFinding]:
    legs = front.get("entry_legs")
    if front.get("trade_execution_state") == "none":
        return []
    if not isinstance(legs, list) or not legs:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.entry-legs-required",
                message="trade records with position lifecycle require entry_legs",
                location="entry_legs",
            )
        ]
    return []


def _check_kill_switches(path: Path, front: Mapping[str, object]) -> list[ValidationFinding]:
    policy = _load_policy(path, front)
    validator_configs = _validator_configs(policy)
    if not validator_configs:
        return []
    event_payload = _load_events_calendar(path, front)
    events = [event for event in as_list(event_payload.get("events")) if isinstance(event, Mapping)]
    checked = as_mapping(front.get("kill_switch_check"))
    at = _first_order_event_at(front)
    findings: list[ValidationFinding] = []
    intent = as_mapping(front.get("order_intent"))
    for key, config in validator_configs:
        callable_id = config.get("validator_callable_id")
        if not isinstance(callable_id, str) or not callable_id:
            continue
        if not has_validator_callable(callable_id):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="trade.kill-switch-callable",
                    message=f"kill switch has no validator implementation: {callable_id}",
                    location=f"policy_snapshot.kill_switch.{key}.validator_callable_id",
                )
            )
            continue
        expected = evaluate_kill_switch(
            callable_id,
            {
                "ticker": front.get("ticker"),
                "at": at,
                "window_days": config.get("window_days"),
                "uses_margin": intent.get("uses_margin"),
            },
            events,
        )
        if expected is None:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="trade.kill-switch-callable",
                    message=f"validator callable did not return a value: {callable_id}",
                    location=f"kill_switch_check.{key}",
                )
            )
            continue
        if callable_id == "no_margin_trading" and expected:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="trade.no-margin-trading",
                    message="margin trading is not allowed by portfolio policy",
                    location="order_intent.uses_margin",
                )
            )
        actual = checked.get(str(key))
        if actual is not expected:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="trade.kill-switch-check",
                    message=f"kill_switch_check.{key} must be {str(expected).lower()}",
                    location=f"kill_switch_check.{key}",
                )
            )
    return findings


def _validator_configs(policy: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    configs: list[tuple[str, Mapping[str, Any]]] = []
    for key, config_value in as_mapping(policy.get("kill_switch")).items():
        config = as_mapping(config_value)
        if config:
            configs.append((str(key), config))
    for item in as_list(policy.get("unique_constraints")):
        if not isinstance(item, Mapping):
            continue
        config = as_mapping(item)
        callable_id = config.get("validator_callable_id")
        key = str(callable_id or item.get("id") or "")
        if key:
            configs.append((key, config))
    return configs


def _load_events_calendar(path: Path, front: Mapping[str, object]) -> Mapping[str, Any]:
    calendars = as_mapping(front.get("calendars_snapshot"))
    try:
        _events_path, payload = load_snapshot_mapping(repo_root_for(path), calendars.get("events"))
        return payload
    except (OSError, ValueError, yaml.YAMLError):
        return {}


def _first_order_event_at(front: Mapping[str, object]) -> str | None:
    for order in as_list(front.get("orders")):
        if not isinstance(order, Mapping):
            continue
        for event in as_list(order.get("events")):
            if not isinstance(event, Mapping):
                continue
            at = event.get("at")
            if isinstance(at, str) and at:
                return at
    return None


def _is_repository_trade_record(path: Path) -> bool:
    root = repo_root_for(path)
    try:
        path.resolve().relative_to((root / "records/06-trades").resolve())
    except ValueError:
        return False
    return True


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _format_path(parts: Iterable[Any]) -> str:
    rendered: list[str] = []
    for part in parts:
        rendered.append(
            f"[{part}]" if isinstance(part, int) else f".{part}" if rendered else str(part)
        )
    return "".join(rendered)
