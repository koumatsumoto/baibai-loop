"""Portfolio exposure snapshot validation."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from .errors import ValidationFinding


def discover_portfolio_exposure_files(root: Path) -> list[Path]:
    """Return portfolio exposure snapshot files."""
    if not root.exists():
        return []
    return sorted(path for path in root.glob("**/*.yaml") if path.is_file())


def validate_portfolio_exposure_file(path: Path) -> list[ValidationFinding]:
    """Validate portfolio exposure budget arithmetic."""
    try:
        raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="portfolio-exposure.parse",
                message=f"failed to read portfolio exposure snapshot: {exc}",
            )
        ]
    if not isinstance(raw, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="portfolio-exposure.root",
                message="portfolio exposure snapshot must be a mapping",
            )
        ]
    findings: list[ValidationFinding] = []
    findings.extend(_check_outstanding_orders(path, raw))
    findings.extend(_check_remaining_budget(path, raw))
    return findings


def _check_outstanding_orders(
    path: Path, snapshot: Mapping[str, object]
) -> list[ValidationFinding]:
    orders = snapshot.get("outstanding_orders")
    if not isinstance(orders, list):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="portfolio-exposure.orders",
                message="outstanding_orders must be a list",
                location="outstanding_orders",
            )
        ]
    findings: list[ValidationFinding] = []
    seen: set[str] = set()
    for index, order in enumerate(orders):
        if not isinstance(order, Mapping):
            findings.append(
                _finding(
                    path,
                    "portfolio-exposure.order",
                    "outstanding order must be a mapping",
                    f"outstanding_orders[{index}]",
                )
            )
            continue
        intent_id = order.get("origin_order_intent_id")
        if not isinstance(intent_id, str) or not intent_id:
            findings.append(
                _finding(
                    path,
                    "portfolio-exposure.order-id",
                    "outstanding order must have origin_order_intent_id",
                    f"outstanding_orders[{index}].origin_order_intent_id",
                )
            )
        elif intent_id in seen:
            findings.append(
                _finding(
                    path,
                    "portfolio-exposure.duplicate-order",
                    "origin_order_intent_id must be unique within a snapshot",
                    f"outstanding_orders[{index}].origin_order_intent_id",
                )
            )
        else:
            seen.add(intent_id)
        if _number(order.get("guarded_notional_yen")) is None:
            findings.append(
                _finding(
                    path,
                    "portfolio-exposure.order-notional",
                    "outstanding order must have guarded_notional_yen",
                    f"outstanding_orders[{index}].guarded_notional_yen",
                )
            )
    return findings


def _check_remaining_budget(path: Path, snapshot: Mapping[str, object]) -> list[ValidationFinding]:
    budget = _number(snapshot.get("tactical_real_budget_yen"))
    remaining = _number(snapshot.get("remaining_tactical_real_budget_yen"))
    orders = snapshot.get("outstanding_orders")
    if budget is None or remaining is None or not isinstance(orders, list):
        return []
    outstanding_notional = sum(
        notional
        for order in orders
        if isinstance(order, Mapping)
        for notional in [_number(order.get("guarded_notional_yen"))]
        if notional is not None
    )
    expected = budget - outstanding_notional
    if abs(remaining - expected) > 1:
        return [
            _finding(
                path,
                "portfolio-exposure.remaining-budget",
                "remaining_tactical_real_budget_yen must equal budget minus outstanding orders",
                "remaining_tactical_real_budget_yen",
            )
        ]
    return []


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _finding(path: Path, code: str, message: str, location: str) -> ValidationFinding:
    return ValidationFinding(
        severity="error",
        target=path,
        code=code,
        message=message,
        location=location,
    )
