"""Payoff-plan arithmetic and corporate-action confirmation checks."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from baibai_loop.foundation.coerce import mapping_or_empty, optional_float
from baibai_loop.foundation.errors import ValidationFinding

from .shared import _close


def _check_payoff(path: Path, front_matter: Mapping[str, object]) -> list[ValidationFinding]:
    payoff = front_matter.get("thesis_payoff")
    if not isinstance(payoff, Mapping):
        return []
    entry = optional_float(payoff.get("max_entry_price_yen"))
    target = optional_float(payoff.get("target_price_yen"))
    stop = optional_float(payoff.get("stop_loss_yen"))
    findings: list[ValidationFinding] = []
    if entry is not None and target is not None and stop is not None:
        # Require positive, ordered prices. The 0 < guard also keeps a malformed
        # entry of 0 from reaching the division below: a validator must report a
        # finding, never crash with ZeroDivisionError on record data.
        if not 0 < stop < entry < target:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="thesis.payoff-order",
                    message=(
                        "long-only payoff must satisfy 0 < stop_loss < max_entry_price "
                        "< target_price"
                    ),
                    location="thesis_payoff",
                )
            )
        if entry > 0:
            expected_upside = round((target / entry - 1) * 100, 2)
            expected_downside = round((1 - stop / entry) * 100, 2)
            risk_reward = (
                round(expected_upside / expected_downside, 2) if expected_downside else None
            )
            if not _close(payoff.get("expected_upside_pct"), expected_upside, tolerance=0.01):
                findings.append(
                    ValidationFinding(
                        severity="error",
                        target=path,
                        code="thesis.expected-upside",
                        message=f"expected_upside_pct must equal {expected_upside}",
                        location="thesis_payoff.expected_upside_pct",
                    )
                )
            if not _close(payoff.get("expected_downside_pct"), expected_downside, tolerance=0.01):
                findings.append(
                    ValidationFinding(
                        severity="error",
                        target=path,
                        code="thesis.expected-downside",
                        message=f"expected_downside_pct must equal {expected_downside}",
                        location="thesis_payoff.expected_downside_pct",
                    )
                )
            if risk_reward is not None and not _close(
                payoff.get("risk_reward_ratio"), risk_reward, tolerance=0.01
            ):
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
