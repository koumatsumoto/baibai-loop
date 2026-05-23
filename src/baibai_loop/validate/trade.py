"""Validate execution records in records/06-trades."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from baibai_loop.policy_config import PORTFOLIO_POLICY

from .domain import (
    as_list,
    as_mapping,
    load_markdown_front_matter,
    load_reference_mapping,
    number,
    repo_root_for,
    repository_ref_error,
    resolve_repository_ref,
)
from .errors import ValidationFinding

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "records" / "_schemas" / "trade.json"

_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
_TICKER_PATTERN = re.compile(r"^[0-9A-Z]{4}$")
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
    findings.extend(_check_reference_refs(path, front))
    findings.extend(_check_ticker(path, front))
    findings.extend(_check_order_ready_shape(path, front))
    findings.extend(_check_trade_safety_gate(path, front))
    findings.extend(_check_order_join(path, front))
    findings.extend(_check_order_state_consistency(path, front))
    findings.extend(_check_current_quantity(path, front))
    findings.extend(_check_guarded_notional(path, front))
    findings.extend(_check_portfolio_concentration(path, front))
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


def _check_reference_refs(path: Path, front: Mapping[str, object]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    specs = {
        "research_ref": (("records/05-research/",), (".md",)),
    }
    for field, (prefixes, suffixes) in specs.items():
        value = front.get(field)
        if isinstance(value, str):
            value = {"ref_path": value}
        findings.extend(
            _check_repository_ref(
                path,
                value,
                location=field,
                code="trade.reference-ref",
                prefixes=prefixes,
                suffixes=suffixes,
            )
        )
    return findings


def _check_repository_ref(
    path: Path,
    value: object,
    *,
    location: str,
    code: str,
    prefixes: tuple[str, ...],
    suffixes: tuple[str, ...],
) -> list[ValidationFinding]:
    if not isinstance(value, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code=code,
                message=f"{location} must be a repository ref mapping",
                location=location,
            )
        ]
    findings: list[ValidationFinding] = []
    root = repo_root_for(path)
    ref = value.get("ref_path")
    error = repository_ref_error(ref, root=root)
    if error is not None:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=code,
                message=error,
                location=f"{location}.ref_path",
            )
        )
        return findings
    assert isinstance(ref, str)
    ref_path = resolve_repository_ref(root, ref)
    if not ref_path.is_file():
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=code,
                message=f"referenced file does not exist: {ref}",
                location=f"{location}.ref_path",
            )
        )
        return findings
    if not ref.startswith(prefixes):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=code,
                message=f"{location}.ref_path must point under {', '.join(prefixes)}",
                location=f"{location}.ref_path",
            )
        )
    if ref_path.suffix not in suffixes:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=code,
                message=f"{location}.ref_path must use suffix {', '.join(suffixes)}",
                location=f"{location}.ref_path",
            )
        )
        return findings
    try:
        if ref_path.suffix == ".md":
            load_reference_mapping(root, value)
        else:
            loaded = yaml.safe_load(ref_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, Mapping):
                raise ValueError("referenced YAML must be a mapping")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=code,
                message=f"referenced file cannot be parsed: {exc}",
                location=f"{location}.ref_path",
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


def _check_order_ready_shape(path: Path, front: Mapping[str, object]) -> list[ValidationFinding]:
    if front.get("trade_execution_state") == "none":
        return []
    findings: list[ValidationFinding] = []
    intent = front.get("order_intent")
    if not isinstance(intent, Mapping):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.order-intent-required",
                message="submitted trade records require order_intent",
                location="order_intent",
            )
        )
    else:
        string_fields = ("order_intent_id", "decision_event_id", "side")
        for field in string_fields:
            value = intent.get(field)
            if not isinstance(value, str) or not value:
                findings.append(
                    ValidationFinding(
                        severity="error",
                        target=path,
                        code="trade.order-intent-field",
                        message=f"order_intent.{field} is required",
                        location=f"order_intent.{field}",
                    )
                )
        if intent.get("side") not in {"buy", "sell"}:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="trade.order-intent-side",
                    message="order_intent.side must be buy or sell",
                    location="order_intent.side",
                )
            )
        for field in ("quantity", "order_price_guard_yen"):
            if _number(intent.get(field)) is None:
                findings.append(
                    ValidationFinding(
                        severity="error",
                        target=path,
                        code="trade.order-intent-field",
                        message=f"order_intent.{field} is required",
                        location=f"order_intent.{field}",
                    )
                )
        quantity = _number(intent.get("quantity"))
        if quantity is not None and quantity <= 0:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="trade.order-intent-quantity",
                    message="trade records with execution intent require order_intent.quantity > 0",
                    location="order_intent.quantity",
                )
            )
        if not isinstance(intent.get("uses_margin"), bool):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="trade.order-intent-field",
                    message="order_intent.uses_margin is required",
                    location="order_intent.uses_margin",
                )
            )

    sizing = front.get("position_sizing_overlay")
    if not isinstance(sizing, Mapping):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.position-sizing-required",
                message="submitted trade records require position_sizing_overlay",
                location="position_sizing_overlay",
            )
        )
    else:
        for field in ("estimated_real_order_notional_yen", "guarded_max_notional_yen"):
            if _number(sizing.get(field)) is None:
                findings.append(
                    ValidationFinding(
                        severity="error",
                        target=path,
                        code="trade.position-sizing-field",
                        message=f"position_sizing_overlay.{field} is required",
                        location=f"position_sizing_overlay.{field}",
                    )
                )

    orders = front.get("orders")
    if not isinstance(orders, list) or not orders:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.orders-required",
                message="submitted trade records require at least one order",
                location="orders",
            )
        )
    return findings


def _check_trade_safety_gate(path: Path, front: Mapping[str, object]) -> list[ValidationFinding]:
    if front.get("trade_execution_state") == "none":
        return []
    findings: list[ValidationFinding] = []
    research = _load_referenced_research(path, front)
    if research is None:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.research-ref-load",
                message="trade records with execution intent require a readable research_ref",
                location="research_ref",
            )
        )
    elif as_mapping(research.get("research_decision")).get("outcome") != "approved":
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.research-approval",
                message="trade records with execution intent require approved research_ref",
                location="research_ref",
            )
        )
    intent = as_mapping(front.get("order_intent"))
    if intent.get("uses_margin") is True:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.no-margin-trading",
                message="margin trading is not allowed",
                location="order_intent.uses_margin",
            )
        )
    return findings


def _load_referenced_research(
    path: Path, front: Mapping[str, object]
) -> Mapping[str, object] | None:
    root = repo_root_for(path)
    research_ref = front.get("research_ref")
    if not isinstance(research_ref, str):
        return None
    if repository_ref_error(research_ref, root=root) is not None:
        return None
    research_path = resolve_repository_ref(root, research_ref)
    if not research_ref.startswith("records/05-research/") or research_path.suffix != ".md":
        return None
    if not research_path.is_file():
        return None
    try:
        return load_markdown_front_matter(research_path)
    except (OSError, ValueError, yaml.YAMLError):
        return None


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


def _check_portfolio_concentration(
    path: Path, front: Mapping[str, object]
) -> list[ValidationFinding]:
    current_notional = _open_trade_notional(front)
    if current_notional is None or current_notional <= 0:
        return []
    capital = as_mapping(PORTFOLIO_POLICY.get("capital_basis"))
    risk = as_mapping(PORTFOLIO_POLICY.get("risk_budget"))
    real_capital_yen = number(capital.get("real_capital_yen"))
    tactical_budget_yen = number(capital.get("tactical_real_budget_yen"))
    if real_capital_yen is None and tactical_budget_yen is None:
        return []
    exposures = _open_trade_exposures(repo_root_for(path))
    if not exposures:
        return []

    findings: list[ValidationFinding] = []
    total = sum(float(item["notional"]) for item in exposures)
    if tactical_budget_yen is not None and total > tactical_budget_yen + 1:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="trade.portfolio-tactical-budget",
                message=(
                    "open trade exposure exceeds portfolio policy tactical_real_budget_yen "
                    f"({total:g} > {tactical_budget_yen:g})"
                ),
                location="position_sizing_overlay",
            )
        )
    if real_capital_yen is None or real_capital_yen <= 0:
        return findings

    checks = (
        ("ticker", "max_ticker_real_concentration_pct", "trade.portfolio-ticker-cap"),
        ("sector_33", "max_sector_real_concentration_pct", "trade.portfolio-sector-cap"),
        ("playbook_id", "max_playbook_real_concentration_pct", "trade.portfolio-playbook-cap"),
    )
    for field, cap_field, code in checks:
        cap_pct = number(risk.get(cap_field))
        if cap_pct is None:
            continue
        cap_yen = real_capital_yen * cap_pct / 100
        grouped: dict[str, float] = {}
        for item in exposures:
            key = str(item.get(field) or "")
            if key:
                grouped[key] = grouped.get(key, 0.0) + float(item["notional"])
        for key, notional in sorted(grouped.items()):
            if notional <= cap_yen + 1:
                continue
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code=code,
                    message=(
                        f"{field}={key} open exposure exceeds {cap_field} "
                        f"({notional:g} > {cap_yen:g})"
                    ),
                    location="position_sizing_overlay",
                )
            )
    return findings


def _open_trade_exposures(root: Path) -> list[dict[str, float | str]]:
    trades_root = root / "records/06-trades"
    if not trades_root.is_dir():
        return []
    exposures: list[dict[str, float | str]] = []
    for trade_path in sorted(trades_root.rglob("*.md")):
        if trade_path.name.startswith("template"):
            continue
        try:
            front = load_markdown_front_matter(trade_path)
        except (OSError, ValueError, yaml.YAMLError):
            continue
        notional = _open_trade_notional(front)
        if notional is None or notional <= 0:
            continue
        sector_33 = ""
        research_ref = front.get("research_ref")
        if isinstance(research_ref, str):
            try:
                research_front = load_markdown_front_matter(root / research_ref)
            except (OSError, ValueError, yaml.YAMLError):
                research_front = {}
            sector_33 = str(research_front.get("sector_33") or "")
        exposures.append(
            {
                "ticker": str(front.get("ticker") or ""),
                "sector_33": sector_33,
                "playbook_id": str(front.get("playbook_id") or ""),
                "notional": notional,
            }
        )
    return exposures


def _open_trade_notional(front: Mapping[str, Any]) -> float | None:
    if front.get("position_state") == "closed":
        return None
    state = front.get("trade_execution_state")
    if front.get("position_state") != "open" and state not in {
        "submitted",
        "partially_filled",
        "filled",
    }:
        return None
    entry_notional = 0.0
    for leg in as_list(front.get("entry_legs")):
        leg_map = as_mapping(leg)
        quantity = number(leg_map.get("quantity"))
        price = number(leg_map.get("average_price_yen"))
        if quantity is not None and price is not None:
            entry_notional += quantity * price
    if entry_notional > 0:
        return entry_notional
    sizing = as_mapping(front.get("position_sizing_overlay"))
    fallback = number(sizing.get("estimated_real_order_notional_yen")) or number(
        sizing.get("guarded_max_notional_yen")
    )
    if fallback is not None:
        return fallback
    intent = as_mapping(front.get("order_intent"))
    quantity = number(intent.get("quantity"))
    guard = number(intent.get("order_price_guard_yen"))
    if quantity is not None and guard is not None:
        return quantity * guard
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
