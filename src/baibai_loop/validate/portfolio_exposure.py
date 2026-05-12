"""Portfolio exposure file validation."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from .domain import (
    load_markdown_front_matter,
    number,
    repo_root_for,
    repository_ref_error,
    resolve_repository_ref,
)
from .errors import ValidationFinding

SCHEMA_PATH = (
    Path(__file__).resolve().parents[3] / "records" / "_schemas" / "portfolio-exposure.json"
)
_REMOVED_HASH_FIELDS = frozenset({"content_" + "sha256", "row_" + "sha256"})
_REMOVED_REFERENCE_FIELDS = frozenset(
    {
        "playbook_snapshot",
        "policy_snapshot",
        "portfolio_exposure_snapshot_ref",
        "calendars_snapshot",
        "universe_snapshot_ref",
        "input_snapshots",
        "screening_rules_snapshot",
        "metric_catalog_snapshot",
        "cache_manifest_hash",
        "snapshot_path",
        "latest_snapshot",
    }
)


def _load_validator() -> Draft202012Validator:
    raw = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"unexpected schema root: {SCHEMA_PATH}")
    Draft202012Validator.check_schema(raw)
    return Draft202012Validator(raw)


_VALIDATOR = _load_validator()


def discover_portfolio_exposure_files(root: Path) -> list[Path]:
    """Return portfolio exposure files."""
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
                message=f"failed to read portfolio exposure file: {exc}",
            )
        ]
    if not isinstance(raw, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="portfolio-exposure.root",
                message="portfolio exposure file must be a mapping",
            )
        ]
    findings: list[ValidationFinding] = []
    findings.extend(_validate_schema(path, raw))
    findings.extend(_check_removed_hash_fields_recursive(path, raw))
    findings.extend(_check_outstanding_orders(path, raw))
    findings.extend(_check_remaining_budget(path, raw))
    findings.extend(_check_rebuild_from_sources(path, raw))
    findings.extend(_check_decision_register_sources(path, raw))
    findings.extend(_check_cap_remaining_fields(path, raw))
    return findings


def _validate_schema(path: Path, snapshot: Mapping[str, object]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for error in _VALIDATOR.iter_errors(snapshot):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=f"portfolio-exposure.{error.validator or 'invalid'}",
                message=str(error.message),
                location=_format_path(error.absolute_path),
            )
        )
    return findings


def _check_removed_hash_fields_recursive(
    path: Path, value: object, *, prefix: str = ""
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    if isinstance(value, Mapping):
        findings.extend(_check_removed_hash_fields(path, value, prefix))
        findings.extend(_check_removed_reference_fields(path, value, prefix))
        for key, child in value.items():
            location = f"{prefix}.{key}" if prefix else str(key)
            findings.extend(_check_removed_hash_fields_recursive(path, child, prefix=location))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            location = f"{prefix}[{index}]" if prefix else f"[{index}]"
            findings.extend(_check_removed_hash_fields_recursive(path, child, prefix=location))
    return findings


def _check_removed_reference_fields(
    path: Path,
    value: Mapping[str, object],
    location: str,
) -> list[ValidationFinding]:
    return [
        _finding(
            path,
            "portfolio-exposure.removed-reference-field",
            f"{field} has been replaced by repository reference fields",
            f"{location}.{field}" if location else field,
        )
        for field in sorted(_REMOVED_REFERENCE_FIELDS)
        if field in value
    ]


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


def _check_decision_register_sources(
    path: Path, snapshot: Mapping[str, object]
) -> list[ValidationFinding]:
    orders = snapshot.get("outstanding_orders")
    if not isinstance(orders, list):
        return []
    outstanding_intents = {
        str(order.get("origin_order_intent_id"))
        for order in orders
        if isinstance(order, Mapping)
        and isinstance(order.get("origin_order_intent_id"), str)
        and order.get("origin_order_intent_id")
    }
    if not outstanding_intents:
        return _validate_decision_register_ref_shapes(path, snapshot)
    refs = snapshot.get("source_decision_register_refs")
    if not isinstance(refs, list) or not refs:
        return [
            _finding(
                path,
                "portfolio-exposure.source-decision-register-required",
                "snapshot with outstanding orders must include source_decision_register_refs",
                "source_decision_register_refs",
            )
        ]
    root = repo_root_for(path)
    findings: list[ValidationFinding] = []
    source_intents: set[str] = set()
    for index, ref in enumerate(refs):
        if not isinstance(ref, Mapping):
            findings.append(
                _finding(
                    path,
                    "portfolio-exposure.source-decision-register-ref",
                    "source_decision_register_refs entries must be mappings",
                    f"source_decision_register_refs[{index}]",
                )
            )
            continue
        ref_path = ref.get("ref_path")
        decision_event_id = ref.get("decision_event_id")
        findings.extend(
            _check_removed_hash_fields(path, ref, f"source_decision_register_refs[{index}]")
        )
        ref_error = repository_ref_error(ref_path, root=root)
        if ref_error is not None or not isinstance(decision_event_id, str):
            findings.append(
                _finding(
                    path,
                    "portfolio-exposure.source-decision-register-ref",
                    "source decision ref requires repository-relative ref_path "
                    "and decision_event_id",
                    f"source_decision_register_refs[{index}]",
                )
            )
            continue
        assert isinstance(ref_path, str)
        if not ref_path.startswith("records/_ledger/") or Path(ref_path).suffix != ".jsonl":
            findings.append(
                _finding(
                    path,
                    "portfolio-exposure.source-decision-register-ref",
                    "source decision ref must point under records/_ledger/ and use .jsonl",
                    f"source_decision_register_refs[{index}].ref_path",
                )
            )
            continue
        ledger_path = resolve_repository_ref(root, ref_path)
        if not ledger_path.is_file():
            findings.append(
                _finding(
                    path,
                    "portfolio-exposure.source-decision-register-missing",
                    f"source decision register does not exist: {ref_path}",
                    f"source_decision_register_refs[{index}].ref_path",
                )
            )
            continue
        row, row_findings = _find_decision_register_row(
            path,
            ledger_path,
            decision_event_id,
            f"source_decision_register_refs[{index}].ref_path",
        )
        findings.extend(row_findings)
        if row_findings:
            continue
        if row is None:
            findings.append(
                _finding(
                    path,
                    "portfolio-exposure.source-decision-register-row-missing",
                    f"decision event not found in source register: {decision_event_id}",
                    f"source_decision_register_refs[{index}].decision_event_id",
                )
            )
            continue
        row_payload = row
        order_intent = row_payload.get("order_intent")
        if not isinstance(order_intent, Mapping):
            findings.append(
                _finding(
                    path,
                    "portfolio-exposure.source-decision-register-intent",
                    "source decision row must include order_intent",
                    f"source_decision_register_refs[{index}].decision_event_id",
                )
            )
            continue
        order_intent_id = order_intent.get("order_intent_id")
        if isinstance(order_intent_id, str) and order_intent_id:
            source_intents.add(order_intent_id)
    if source_intents != outstanding_intents:
        findings.append(
            _finding(
                path,
                "portfolio-exposure.source-decision-register-set",
                "source_decision_register_refs must cover every outstanding order intent exactly",
                "source_decision_register_refs",
            )
        )
    return findings


def _validate_decision_register_ref_shapes(
    path: Path, snapshot: Mapping[str, object]
) -> list[ValidationFinding]:
    refs = snapshot.get("source_decision_register_refs")
    if refs is None:
        return []
    if not isinstance(refs, list):
        return [
            _finding(
                path,
                "portfolio-exposure.source-decision-register-ref",
                "source_decision_register_refs must be a list",
                "source_decision_register_refs",
            )
        ]
    root = repo_root_for(path)
    findings: list[ValidationFinding] = []
    for index, ref in enumerate(refs):
        if not isinstance(ref, Mapping):
            findings.append(
                _finding(
                    path,
                    "portfolio-exposure.source-decision-register-ref",
                    "source_decision_register_refs entries must be mappings",
                    f"source_decision_register_refs[{index}]",
                )
            )
            continue
        ref_path = ref.get("ref_path")
        decision_event_id = ref.get("decision_event_id")
        findings.extend(
            _check_removed_hash_fields(path, ref, f"source_decision_register_refs[{index}]")
        )
        ref_error = repository_ref_error(ref_path, root=root)
        if ref_error is not None or not isinstance(decision_event_id, str):
            findings.append(
                _finding(
                    path,
                    "portfolio-exposure.source-decision-register-ref",
                    "source decision ref requires repository-relative ref_path "
                    "and decision_event_id",
                    f"source_decision_register_refs[{index}]",
                )
            )
            continue
        assert isinstance(ref_path, str)
        if not ref_path.startswith("records/_ledger/") or Path(ref_path).suffix != ".jsonl":
            findings.append(
                _finding(
                    path,
                    "portfolio-exposure.source-decision-register-ref",
                    "source decision ref must point under records/_ledger/ and use .jsonl",
                    f"source_decision_register_refs[{index}].ref_path",
                )
            )
            continue
        ledger_path = resolve_repository_ref(root, ref_path)
        if not ledger_path.is_file():
            findings.append(
                _finding(
                    path,
                    "portfolio-exposure.source-decision-register-missing",
                    f"source decision register does not exist: {ref_path}",
                    f"source_decision_register_refs[{index}].ref_path",
                )
            )
    return findings


def _find_decision_register_row(
    path: Path,
    ledger_path: Path,
    decision_event_id: str,
    location: str,
) -> tuple[Mapping[str, object] | None, list[ValidationFinding]]:
    findings: list[ValidationFinding] = []
    try:
        lines = ledger_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return None, [
            _finding(
                path,
                "portfolio-exposure.source-decision-register-parse",
                f"failed to read source decision register: {exc}",
                location,
            )
        ]
    matched: Mapping[str, object] | None = None
    for line_no, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            findings.append(
                _finding(
                    path,
                    "portfolio-exposure.source-decision-register-parse",
                    f"source decision register JSONL parse failed at line {line_no}: {exc}",
                    location,
                )
            )
            continue
        if not isinstance(payload, Mapping):
            findings.append(
                _finding(
                    path,
                    "portfolio-exposure.source-decision-register-parse",
                    f"source decision register line {line_no} must be an object",
                    location,
                )
            )
            continue
        if payload.get("decision_event_id") == decision_event_id:
            matched = payload
    if findings:
        return None, findings
    if matched is not None:
        return matched, []
    return None, []


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


def _check_rebuild_from_sources(
    path: Path, snapshot: Mapping[str, object]
) -> list[ValidationFinding]:
    source_refs = snapshot.get("source_trade_refs")
    orders = snapshot.get("outstanding_orders")
    root = repo_root_for(path)
    expected_from_all_trades, all_trade_findings = _all_outstanding_orders_as_of(
        root, snapshot.get("as_of"), target=path, include_source=True
    )
    findings: list[ValidationFinding] = list(all_trade_findings)
    if not isinstance(orders, list):
        return findings
    if not isinstance(source_refs, list) or not source_refs:
        if not orders and not expected_from_all_trades:
            return findings
        findings.append(
            _finding(
                path,
                "portfolio-exposure.source-trades-required",
                "snapshot with outstanding orders must include source_trade_refs",
                "source_trade_refs",
            )
        )
        return findings
    rebuilt: list[dict[str, object]] = []
    source_paths: set[str] = set()
    for index, ref in enumerate(source_refs):
        if not isinstance(ref, Mapping):
            findings.append(
                _finding(
                    path,
                    "portfolio-exposure.source-trade-ref",
                    "source_trade_refs entries must be mappings",
                    f"source_trade_refs[{index}]",
                )
            )
            continue
        ref_path = ref.get("ref_path")
        findings.extend(_check_removed_hash_fields(path, ref, f"source_trade_refs[{index}]"))
        ref_error = repository_ref_error(ref_path, root=root)
        if ref_error is not None:
            findings.append(
                _finding(
                    path,
                    "portfolio-exposure.source-trade-ref",
                    "source trade ref requires repository-relative ref_path",
                    f"source_trade_refs[{index}]",
                )
            )
            continue
        assert isinstance(ref_path, str)
        if not ref_path.startswith("records/06-trades/") or Path(ref_path).suffix != ".md":
            findings.append(
                _finding(
                    path,
                    "portfolio-exposure.source-trade-ref",
                    "source trade ref must point under records/06-trades/ and use .md",
                    f"source_trade_refs[{index}].ref_path",
                )
            )
            continue
        source_paths.add(ref_path)
        trade_path = resolve_repository_ref(root, ref_path)
        if not trade_path.is_file():
            findings.append(
                _finding(
                    path,
                    "portfolio-exposure.source-trade-missing",
                    f"source trade record does not exist: {ref_path}",
                    f"source_trade_refs[{index}].ref_path",
                )
            )
            continue
        try:
            trade = load_markdown_front_matter(trade_path)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            findings.append(
                _finding(
                    path,
                    "portfolio-exposure.source-trade-parse",
                    f"failed to read source trade: {exc}",
                    f"source_trade_refs[{index}]",
                )
            )
            continue
        rebuilt.extend(_outstanding_orders_from_trade(trade, snapshot.get("as_of")))
    expected_source_paths = {
        str(item["_source_path"]) for item in expected_from_all_trades if "_source_path" in item
    }
    if expected_source_paths and source_paths != expected_source_paths:
        findings.append(
            _finding(
                path,
                "portfolio-exposure.source-trade-set",
                "source_trade_refs must include every trade record with outstanding orders as_of",
                "source_trade_refs",
            )
        )
    committed = sorted(
        (_normalize_order(order) for order in orders if isinstance(order, Mapping)),
        key=lambda item: str(item.get("origin_order_intent_id")),
    )
    expected = sorted(
        (_without_source(item) for item in expected_from_all_trades),
        key=lambda item: str(item.get("origin_order_intent_id")),
    )
    if not expected:
        expected = sorted(rebuilt, key=lambda item: str(item.get("origin_order_intent_id")))
    if committed != expected:
        findings.append(
            _finding(
                path,
                "portfolio-exposure.rebuild",
                "outstanding_orders must rebuild exactly from source_trade_refs",
                "outstanding_orders",
            )
        )
    return findings


def _check_cap_remaining_fields(
    path: Path,
    snapshot: Mapping[str, object],
) -> list[ValidationFinding]:
    policy, policy_findings = _load_policy_ref(path, snapshot.get("as_of"))
    findings: list[ValidationFinding] = list(policy_findings)
    if not policy:
        return findings
    capital = policy.get("capital_basis")
    risk = policy.get("risk_budget")
    if not isinstance(capital, Mapping) or not isinstance(risk, Mapping):
        return []
    real_capital = number(capital.get("real_capital_yen"))
    paper_capital = number(capital.get("paper_proxy_capital_yen"))
    if real_capital is None or paper_capital is None:
        return []
    outstanding = snapshot.get("outstanding_orders")
    if not isinstance(outstanding, list):
        return []
    outstanding_orders = [item for item in outstanding if isinstance(item, Mapping)]
    total_real_outstanding = _sum_notional(outstanding_orders)
    playbook_outstanding = _max_group_notional(outstanding_orders, "playbook_id")
    exposure_outstanding = _max_group_notional(outstanding_orders, "economic_exposure_group")
    expected = {
        "ticker_paper_proxy_cap_remaining_yen": number(
            risk.get("max_paper_proxy_position_size_yen")
        ),
        "sector_paper_proxy_cap_remaining_yen": paper_capital,
        "playbook_paper_proxy_cap_remaining_yen": paper_capital,
        "economic_exposure_paper_proxy_cap_remaining_yen": paper_capital,
        "ticker_real_cap_remaining_yen": _pct_cap(
            real_capital, risk.get("max_ticker_real_concentration_pct")
        ),
        "sector_real_cap_remaining_yen": _remaining_pct_cap(
            real_capital,
            risk.get("max_sector_real_concentration_pct"),
            total_real_outstanding,
        ),
        "playbook_real_cap_remaining_yen": _remaining_pct_cap(
            real_capital,
            risk.get("max_playbook_real_concentration_pct"),
            playbook_outstanding,
        ),
        "economic_exposure_real_cap_remaining_yen": _remaining_pct_cap(
            real_capital,
            risk.get("max_economic_exposure_real_concentration_pct"),
            exposure_outstanding,
        ),
    }
    for field, expected_value in expected.items():
        if expected_value is None:
            continue
        actual = number(snapshot.get(field))
        if actual is None or abs(actual - expected_value) > 1:
            findings.append(
                _finding(
                    path,
                    "portfolio-exposure.cap-remaining",
                    f"{field} must rebuild to {expected_value:g} from policy and orders",
                    field,
                )
            )
    return findings


def _all_outstanding_orders_as_of(
    root: Path,
    as_of_value: object,
    *,
    target: Path,
    include_source: bool = False,
) -> tuple[list[dict[str, object]], list[ValidationFinding]]:
    as_of = _parse_datetime(as_of_value)
    result: list[dict[str, object]] = []
    findings: list[ValidationFinding] = []
    trades_root = root / "records/06-trades"
    if not trades_root.is_dir():
        return result, findings
    for trade_path in sorted(trades_root.rglob("*.md")):
        location = str(trade_path.relative_to(root))
        try:
            trade = load_markdown_front_matter(trade_path)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            findings.append(
                _finding(
                    target,
                    "portfolio-exposure.trade-source-parse",
                    f"failed to parse trade source: {exc}",
                    location,
                )
            )
            continue
        for order in _outstanding_orders_from_trade(trade, as_of):
            if include_source:
                order["_source_path"] = str(trade_path.relative_to(root))
            result.append(order)
    return result, findings


def _outstanding_orders_from_trade(
    trade: Mapping[str, object],
    as_of_value: object,
) -> list[dict[str, object]]:
    as_of = _parse_datetime(as_of_value)
    result: list[dict[str, object]] = []
    orders = trade.get("orders")
    if not isinstance(orders, list):
        return result
    for order in orders:
        if not isinstance(order, Mapping):
            continue
        state = order.get("state")
        if state not in {"submitted", "partially_filled"}:
            continue
        submitted_at = _order_submitted_at(order)
        if as_of is not None and submitted_at is not None and submitted_at > as_of:
            continue
        intent_id = order.get("origin_order_intent_id")
        guard = number(order.get("order_price_guard_yen"))
        submitted = number(order.get("submitted_quantity"))
        filled = number(order.get("filled_quantity")) or 0
        if not isinstance(intent_id, str) or guard is None or submitted is None:
            continue
        remaining_quantity = max(submitted - filled, 0)
        if remaining_quantity <= 0:
            continue
        result.append(
            {
                "origin_order_intent_id": intent_id,
                "ticker": trade.get("ticker"),
                "side": order.get("side"),
                "guarded_notional_yen": int(remaining_quantity * guard),
                "playbook_id": _trade_playbook_id(trade),
                "economic_exposure_group": _trade_playbook_id(trade),
            }
        )
    return result


def _order_submitted_at(order: Mapping[str, object]) -> datetime | None:
    events = order.get("events")
    if not isinstance(events, list):
        return None
    for event in events:
        if not isinstance(event, Mapping) or event.get("event_type") != "submit":
            continue
        parsed = _parse_datetime(event.get("at"))
        if parsed is not None:
            return parsed
    return None


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone(timedelta(hours=9)))
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=9)))
    return parsed


def _trade_playbook_id(trade: Mapping[str, object]) -> str | None:
    value = trade.get("playbook_id")
    return str(value) if isinstance(value, str) else None


def _normalize_order(order: Mapping[str, object]) -> dict[str, object]:
    return {
        "origin_order_intent_id": order.get("origin_order_intent_id"),
        "ticker": order.get("ticker"),
        "side": order.get("side"),
        "guarded_notional_yen": int(number(order.get("guarded_notional_yen")) or 0),
        "playbook_id": order.get("playbook_id"),
        "economic_exposure_group": order.get("economic_exposure_group"),
    }


def _without_source(order: Mapping[str, object]) -> dict[str, object]:
    return dict(_normalize_order(order))


def _load_policy_ref(
    path: Path, as_of_value: object
) -> tuple[Mapping[str, object], list[ValidationFinding]]:
    root = repo_root_for(path)
    as_of = _parse_datetime(as_of_value)
    policy_root = root / "records/01-policy/2026"
    best_path: Path | None = None
    best_effective: datetime | None = None
    findings: list[ValidationFinding] = []
    for candidate in sorted(policy_root.rglob("*.md")):
        location = str(candidate.relative_to(root))
        try:
            payload = load_markdown_front_matter(candidate)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            findings.append(
                _finding(
                    path,
                    "portfolio-exposure.policy-ref-parse",
                    f"failed to parse policy source: {exc}",
                    location,
                )
            )
            continue
        effective = _parse_datetime(payload.get("effective_from"))
        if effective is None:
            continue
        if as_of is not None and effective > as_of:
            continue
        if best_effective is None or effective > best_effective:
            best_path = candidate
            best_effective = effective
    if best_path is None:
        return {}, findings
    try:
        return load_markdown_front_matter(best_path), findings
    except (OSError, ValueError, yaml.YAMLError) as exc:
        findings.append(
            _finding(
                path,
                "portfolio-exposure.policy-ref-parse",
                f"failed to parse selected policy source: {exc}",
                str(best_path.relative_to(root)),
            )
        )
        return {}, findings


def _sum_notional(orders: list[Mapping[str, object]]) -> float:
    return sum(number(order.get("guarded_notional_yen")) or 0 for order in orders)


def _max_group_notional(orders: list[Mapping[str, object]], field: str) -> float:
    totals: dict[str, float] = {}
    for order in orders:
        key = order.get(field)
        if not isinstance(key, str) or not key:
            continue
        totals[key] = totals.get(key, 0.0) + (number(order.get("guarded_notional_yen")) or 0)
    return max(totals.values(), default=0.0)


def _pct_cap(base: float, pct_value: object) -> float | None:
    pct = number(pct_value)
    if pct is None:
        return None
    return base * pct / 100


def _remaining_pct_cap(base: float, pct_value: object, used: float) -> float | None:
    cap = _pct_cap(base, pct_value)
    if cap is None:
        return None
    return max(cap - used, 0)


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


def _format_path(parts: Iterable[object]) -> str:
    rendered: list[str] = []
    for part in parts:
        rendered.append(
            f"[{part}]" if isinstance(part, int) else f".{part}" if rendered else str(part)
        )
    return "".join(rendered)


def _check_removed_hash_fields(
    path: Path,
    value: Mapping[str, object],
    location: str,
) -> list[ValidationFinding]:
    return [
        _finding(
            path,
            "portfolio-exposure.removed-hash-field",
            f"{field} is no longer allowed in repository links",
            f"{location}.{field}" if location else field,
        )
        for field in sorted(_REMOVED_HASH_FIELDS)
        if field in value
    ]
