"""Payoff-plan arithmetic, long-hold requirements, and corporate-action checks."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from baibai_loop.foundation.coerce import list_or_empty, mapping_or_empty, optional_float
from baibai_loop.foundation.errors import ValidationFinding

from .shared import _LONG_HOLD_EFFECTIVE_DATE, _close, _gate_boundary_date


def _check_payoff(path: Path, front_matter: Mapping[str, object]) -> list[ValidationFinding]:
    payoff = front_matter.get("thesis_payoff")
    if not isinstance(payoff, Mapping):
        return []
    findings: list[ValidationFinding] = []
    entry = optional_float(payoff.get("max_entry_price_yen"))
    fair_value = optional_float(payoff.get("fair_value_yen"))
    downside = optional_float(payoff.get("expected_downside_pct"))

    # Prices must be positive when present. The > 0 guard also keeps a malformed
    # entry of 0 from reaching the division below: a validator must report a
    # finding, never crash with ZeroDivisionError on record data.
    for field, value in (("max_entry_price_yen", entry), ("fair_value_yen", fair_value)):
        if value is not None and value <= 0:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="thesis.payoff-price",
                    message=f"thesis_payoff.{field} must be positive",
                    location=f"thesis_payoff.{field}",
                )
            )
    if downside is not None and downside <= 0:
        # long-only の下方は正の % で表す (0 以下だと risk_reward_ratio が定義できない)。
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="thesis.expected-downside",
                message="expected_downside_pct must be a positive percentage",
                location="thesis_payoff.expected_downside_pct",
            )
        )
    if findings:
        return findings

    # expected_upside は FV と entry から導出する。downside は保守的な下値までの
    # 判断値 (式では導出しない)、RR は両者の比として検算する。
    if entry is not None and fair_value is not None:
        expected_upside = round((fair_value / entry - 1) * 100, 2)
        if not _close(payoff.get("expected_upside_pct"), expected_upside, tolerance=0.01):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="thesis.expected-upside",
                    message=(
                        f"expected_upside_pct must equal {expected_upside} "
                        "((fair_value_yen / max_entry_price_yen - 1) * 100)"
                    ),
                    location="thesis_payoff.expected_upside_pct",
                )
            )
    upside = optional_float(payoff.get("expected_upside_pct"))
    if upside is not None and downside is not None:
        risk_reward = round(upside / downside, 2)
        if not _close(payoff.get("risk_reward_ratio"), risk_reward, tolerance=0.01):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="thesis.risk-reward",
                    message=f"risk_reward_ratio must equal {risk_reward}",
                    location="thesis_payoff.risk_reward_ratio",
                )
            )
    return findings


def _check_long_hold_requirements(
    path: Path, front_matter: Mapping[str, object]
) -> list[ValidationFinding]:
    """Require the long-hold estimate fields on newly approved records.

    買いは「割安ゾーン ∧ FV 下方乖離」、売りは「割高化 / fundamental 毀損」の
    2 トリガーのみ (docs/workflow/research.md)。その判断が calibrate 可能に
    なるよう、approved record には FV・下方見積り・期待利回り・毀損条件・
    塩漬け耐性ゲートの記入を要求する (耐性の合否判定は人間が行う)。
    """
    decision = mapping_or_empty(front_matter.get("thesis_decision"))
    if decision.get("outcome") != "approved":
        return []
    gate_date = _gate_boundary_date(front_matter, path=path)
    if gate_date is None or gate_date < _LONG_HOLD_EFFECTIVE_DATE:
        return []

    findings: list[ValidationFinding] = []
    payoff = mapping_or_empty(front_matter.get("thesis_payoff"))
    entry = optional_float(payoff.get("max_entry_price_yen"))
    fair_value = optional_float(payoff.get("fair_value_yen"))
    if entry is None:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="thesis.max-entry-required",
                message="approved thesis requires thesis_payoff.max_entry_price_yen",
                location="thesis_payoff.max_entry_price_yen",
            )
        )
    if fair_value is None:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="thesis.fair-value-required",
                message="approved thesis requires thesis_payoff.fair_value_yen",
                location="thesis_payoff.fair_value_yen",
            )
        )
    elif entry is not None and fair_value <= entry:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="thesis.fair-value-upside",
                message=(
                    "approved long-hold entry requires fair_value_yen > max_entry_price_yen "
                    "(買いは割安ゾーン ∧ FV 下方乖離)"
                ),
                location="thesis_payoff.fair_value_yen",
            )
        )
    if optional_float(payoff.get("expected_downside_pct")) is None:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="thesis.expected-downside-required",
                message="approved thesis requires thesis_payoff.expected_downside_pct",
                location="thesis_payoff.expected_downside_pct",
            )
        )
    if optional_float(payoff.get("expected_yield_pct")) is None:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="thesis.expected-yield-required",
                message=(
                    "approved thesis requires thesis_payoff.expected_yield_pct "
                    "(FV 収束 + income の total-return 年率概算)"
                ),
                location="thesis_payoff.expected_yield_pct",
            )
        )
    conditions = [
        item
        for item in list_or_empty(payoff.get("invalidation_conditions"))
        if isinstance(item, str) and item.strip()
    ]
    if not conditions:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="thesis.invalidation-conditions-required",
                message=(
                    "approved thesis requires non-empty thesis_payoff.invalidation_conditions "
                    "(保有中に監視する fundamental 毀損条件)"
                ),
                location="thesis_payoff.invalidation_conditions",
            )
        )
    if not isinstance(front_matter.get("durability_gate"), Mapping):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="thesis.durability-gate-required",
                message=(
                    "approved thesis requires durability_gate "
                    "(塩漬け耐性の記入。合否判定は人間が行う)"
                ),
                location="durability_gate",
            )
        )
    return findings


def _check_corporate_action_check(
    path: Path, front_matter: Mapping[str, object]
) -> list[ValidationFinding]:
    decision = mapping_or_empty(front_matter.get("thesis_decision"))
    if decision.get("outcome") != "approved":
        return []
    check = front_matter.get("corporate_action_check")
    if not isinstance(check, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="thesis.corporate-action-check",
                message="approved thesis requires corporate_action_check",
                location="corporate_action_check",
            )
        ]
    if check.get("checked") is not True:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="thesis.corporate-action-check",
                message="corporate_action_check.checked must be true for approved thesis",
                location="corporate_action_check.checked",
            )
        ]
    result = check.get("result")
    if result not in {"none", "found", "not_applicable"}:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="thesis.corporate-action-check-result",
                message="corporate_action_check.result must be none, found, or not_applicable",
                location="corporate_action_check.result",
            )
        ]
    if result != "none":
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="thesis.corporate-action-check-result",
                message="approved thesis requires corporate_action_check.result to be none",
                location="corporate_action_check.result",
            )
        ]
    return []
