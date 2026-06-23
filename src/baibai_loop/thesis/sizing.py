"""Position-sizing invariants and approved-decision sizing limits."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from baibai_loop.foundation.coerce import mapping_or_empty, optional_float
from baibai_loop.foundation.errors import ValidationFinding
from baibai_loop.position.policy import PORTFOLIO_POLICY

from .shared import _close

_CANONICAL_SIZING_FIELDS = {
    "paper_proxy_position_size_yen",
    "real_order_intent_yen",
    "adv_participation_pct",
}


def _check_sizing_invariants(
    path: Path, front_matter: Mapping[str, object]
) -> list[ValidationFinding]:
    raw_sizing = front_matter.get("position_sizing_overlay")
    decision = mapping_or_empty(front_matter.get("thesis_decision"))
    if not isinstance(raw_sizing, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="thesis.position-sizing-shape",
                message="position_sizing_overlay must be a mapping with canonical sizing fields",
                location="position_sizing_overlay",
            )
        ]
    sizing = raw_sizing
    findings = _check_position_sizing_overlay_shape(path, front_matter, sizing)
    if decision.get("outcome") != "approved":
        expected_zero = {
            "paper_proxy_position_size_yen": 0,
            "real_order_intent_yen": 0,
            "adv_participation_pct": 0,
        }
        for field, expected_value in expected_zero.items():
            value = sizing.get(field)
            tolerance = 1 if field.endswith("_yen") else 0.0001
            if not _close(value, expected_value, tolerance=tolerance):
                findings.append(
                    ValidationFinding(
                        severity="error",
                        target=path,
                        code="thesis.rejected-sizing",
                        message=f"{field} must be zero for non-approved decisions",
                        location=f"position_sizing_overlay.{field}",
                    )
                )
        return findings

    avg_turnover_oku = optional_float(front_matter.get("avg_turnover_oku"))
    if avg_turnover_oku is None or avg_turnover_oku <= 0:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="thesis.adv-participation-input",
                message="avg_turnover_oku is required to derive adv_participation_pct",
                location="avg_turnover_oku",
            )
        )
        return findings

    return findings + _check_approved_position_sizing_limits(path, front_matter, sizing)


def _check_position_sizing_overlay_shape(
    path: Path,
    front_matter: Mapping[str, object],
    sizing: Mapping[str, object],
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for field in sorted(_CANONICAL_SIZING_FIELDS):
        if field not in sizing:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="thesis.position-sizing-missing-field",
                    message=f"position_sizing_overlay.{field} is required",
                    location=f"position_sizing_overlay.{field}",
                )
            )
    return findings


def _check_approved_position_sizing_limits(
    path: Path,
    front_matter: Mapping[str, object],
    sizing: Mapping[str, object],
    policy: Mapping[str, Any] = PORTFOLIO_POLICY,
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    capital = mapping_or_empty(policy.get("capital_basis"))
    risk = mapping_or_empty(policy.get("risk_budget"))
    scaling = mapping_or_empty(policy.get("execution_scaling"))
    avg_turnover_oku = optional_float(front_matter.get("avg_turnover_oku"))
    paper_yen = optional_float(sizing.get("paper_proxy_position_size_yen"))
    real_intent = optional_float(sizing.get("real_order_intent_yen"))
    adv_participation_pct = optional_float(sizing.get("adv_participation_pct"))

    numeric_fields = {
        "paper_proxy_position_size_yen": paper_yen,
        "real_order_intent_yen": real_intent,
        "adv_participation_pct": adv_participation_pct,
    }
    for field, value in numeric_fields.items():
        if value is None or value < 0:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="thesis.position-sizing-value",
                    message=f"position_sizing_overlay.{field} must be a non-negative number",
                    location=f"position_sizing_overlay.{field}",
                )
            )
    if findings:
        return findings
    assert paper_yen is not None
    assert real_intent is not None
    assert adv_participation_pct is not None
    assert avg_turnover_oku is not None

    if paper_yen <= 0:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="thesis.paper-proxy-position-size",
                message="approved thesis requires positive paper_proxy_position_size_yen",
                location="position_sizing_overlay.paper_proxy_position_size_yen",
            )
        )

    expected_adv = round(
        paper_yen / (avg_turnover_oku * 100_000_000) * 100 if avg_turnover_oku > 0 else 0.0,
        4,
    )
    if not _close(adv_participation_pct, expected_adv, tolerance=0.0001):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="thesis.adv-participation-pct",
                message=(
                    "adv_participation_pct must derive from "
                    "paper_proxy_position_size_yen and avg_turnover_oku"
                ),
                location="position_sizing_overlay.adv_participation_pct",
            )
        )

    scaled_real = paper_yen * (
        (optional_float(scaling.get("paper_to_real_order_notional_pct")) or 0) / 100
    )

    paper_cap = optional_float(risk.get("max_paper_proxy_position_size_yen"))
    if paper_cap is not None and paper_yen > paper_cap:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="thesis.paper-proxy-position-cap",
                message=f"paper_proxy_position_size_yen must not exceed policy cap {paper_cap:g}",
                location="position_sizing_overlay.paper_proxy_position_size_yen",
            )
        )

    adv_cap_pct = optional_float(risk.get("max_adv_participation_pct"))
    liquidity_cap = (
        avg_turnover_oku * 100_000_000 * adv_cap_pct / 100 if adv_cap_pct is not None else None
    )
    real_caps = {
        "paper_to_real_order_notional_pct": scaled_real,
        "tactical_real_budget_yen": optional_float(capital.get("tactical_real_budget_yen")),
        "max_real_order_notional_yen": optional_float(risk.get("max_real_order_notional_yen")),
        "max_adv_participation_pct": liquidity_cap,
    }
    for cap_name, cap_value in real_caps.items():
        if cap_value is not None and real_intent > cap_value + 1:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="thesis.real-order-intent-cap",
                    message=f"real_order_intent_yen must not exceed {cap_name} cap {cap_value:g}",
                    location="position_sizing_overlay.real_order_intent_yen",
                )
            )
    return findings
