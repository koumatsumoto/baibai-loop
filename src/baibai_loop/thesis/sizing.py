"""Position-sizing invariants and approved-decision sizing limits."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from baibai_loop.foundation.coerce import mapping_or_empty, optional_float
from baibai_loop.foundation.errors import ValidationFinding
from baibai_loop.position.policy import PORTFOLIO_POLICY

from .shared import _LONG_HOLD_EFFECTIVE_DATE, _close, _gate_boundary_date

_CANONICAL_SIZING_FIELDS = (
    "adv_participation_pct",
    "estimated_real_order_notional_yen",
)


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
    findings = _check_position_sizing_overlay_shape(path, sizing)
    if decision.get("outcome") != "approved":
        zero_fields = (
            "estimated_real_order_notional_yen",
            "guarded_max_notional_yen",
            "adv_participation_pct",
        )
        for field in zero_fields:
            if field == "guarded_max_notional_yen" and field not in sizing:
                continue
            tolerance = 1 if field.endswith("_yen") else 0.0001
            if not _close(sizing.get(field), 0, tolerance=tolerance):
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
    sizing: Mapping[str, object],
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for field in _CANONICAL_SIZING_FIELDS:
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
    avg_turnover_oku = optional_float(front_matter.get("avg_turnover_oku"))
    estimated = optional_float(sizing.get("estimated_real_order_notional_yen"))
    guarded = optional_float(sizing.get("guarded_max_notional_yen"))
    adv_participation_pct = optional_float(sizing.get("adv_participation_pct"))

    numeric_fields: dict[str, float | None] = {
        "estimated_real_order_notional_yen": estimated,
        "adv_participation_pct": adv_participation_pct,
    }
    if "guarded_max_notional_yen" in sizing:
        numeric_fields["guarded_max_notional_yen"] = guarded
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
    assert estimated is not None
    assert adv_participation_pct is not None
    assert avg_turnover_oku is not None

    if estimated <= 0:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="thesis.order-notional",
                message="approved thesis requires positive estimated_real_order_notional_yen",
                location="position_sizing_overlay.estimated_real_order_notional_yen",
            )
        )

    expected_adv = round(
        estimated / (avg_turnover_oku * 100_000_000) * 100 if avg_turnover_oku > 0 else 0.0,
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
                    "estimated_real_order_notional_yen and avg_turnover_oku"
                ),
                location="position_sizing_overlay.adv_participation_pct",
            )
        )

    # 1 注文サイズの絶対額上限は置かない (docs/portfolio-management.md)。上限は
    # liquidity (ADV 参加率) と ticker concentration の % cap で律速する。
    real_capital_yen = optional_float(capital.get("real_capital_yen"))
    ticker_cap_pct = optional_float(risk.get("max_ticker_real_concentration_pct"))
    adv_cap_pct = optional_float(risk.get("max_adv_participation_pct"))
    order_caps = {
        "max_adv_participation_pct": (
            avg_turnover_oku * 100_000_000 * adv_cap_pct / 100 if adv_cap_pct is not None else None
        ),
        "max_ticker_real_concentration_pct": (
            real_capital_yen * ticker_cap_pct / 100
            if real_capital_yen is not None and ticker_cap_pct is not None
            else None
        ),
    }
    for cap_name, cap_value in order_caps.items():
        if cap_value is not None and estimated > cap_value + 1:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="thesis.order-notional-cap",
                    message=(
                        f"estimated_real_order_notional_yen must not exceed {cap_name} "
                        f"cap {cap_value:g}"
                    ),
                    location="position_sizing_overlay.estimated_real_order_notional_yen",
                )
            )

    gate_date = _gate_boundary_date(front_matter, path=path)
    if gate_date is not None and gate_date >= _LONG_HOLD_EFFECTIVE_DATE:
        if guarded is None:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="thesis.guarded-notional-required",
                    message=(
                        "approved thesis requires position_sizing_overlay."
                        "guarded_max_notional_yen (guard price ベースの発注上限)"
                    ),
                    location="position_sizing_overlay.guarded_max_notional_yen",
                )
            )
        elif guarded <= 0:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="thesis.guarded-notional-required",
                    message="approved thesis requires positive guarded_max_notional_yen",
                    location="position_sizing_overlay.guarded_max_notional_yen",
                )
            )
    return findings
