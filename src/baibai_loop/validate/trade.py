"""Validate execution records in records/06-trades."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from .errors import ValidationFinding

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
    findings.extend(_check_ticker(path, front))
    findings.extend(_check_order_join(path, front))
    findings.extend(_check_order_state_consistency(path, front))
    findings.extend(_check_guarded_notional(path, front))
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
