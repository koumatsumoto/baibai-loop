"""Entry-preflight checks applied to approved act-now decisions."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path

from baibai_loop.foundation.coerce import mapping_or_empty, optional_float
from baibai_loop.foundation.errors import ValidationFinding
from baibai_loop.position.policy import PORTFOLIO_POLICY

from .shared import (
    _LONG_HOLD_EFFECTIVE_DATE,
    _gate_boundary_date,
    _thesis_record_date,
)

_KNOWN_ENTRY_PREFLIGHT_ACTIONS = {"proceed", "starter", "defer"}
_ENTRY_PREFLIGHT_EFFECTIVE_DATE = date(2026, 6, 1)


def _check_entry_preflight(
    path: Path, front_matter: Mapping[str, object]
) -> list[ValidationFinding]:
    decision = front_matter.get("thesis_decision")
    outcome = decision.get("outcome") if isinstance(decision, Mapping) else None
    if outcome != "approved":
        return []
    thesis_date = _thesis_record_date(front_matter, path=path)
    if thesis_date is None or thesis_date < _ENTRY_PREFLIGHT_EFFECTIVE_DATE:
        return []

    preflight = front_matter.get("entry_preflight")
    if not isinstance(preflight, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="thesis.entry-preflight-required",
                message=("approved thesis dated 2026-06-01 or later requires entry_preflight"),
                location="entry_preflight",
            )
        ]

    findings: list[ValidationFinding] = []
    action = preflight.get("action")
    if action not in _KNOWN_ENTRY_PREFLIGHT_ACTIONS:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="thesis.entry-preflight-action",
                message="entry_preflight.action must be proceed, starter, or defer",
                location="entry_preflight.action",
            )
        )
    reason = preflight.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="thesis.entry-preflight-reason",
                message="entry_preflight.reason is required",
                location="entry_preflight.reason",
            )
        )

    # 割安 (相対劣後)を買うのが本流のため、相対リターンや market regime による
    # hard trigger は持たない (docs/workflow/research.md)。Active trade records retain
    # the exposure gate until a canonical portfolio ledger exists.
    hard_triggers: list[str] = []
    gate_date = _gate_boundary_date(front_matter, path=path) or thesis_date
    if gate_date >= _LONG_HOLD_EFFECTIVE_DATE:
        exposure = mapping_or_empty(preflight.get("exposure_after_order"))
        risk = mapping_or_empty(PORTFOLIO_POLICY.get("risk_budget"))
        exposure_caps = (
            ("sector_33_pct", "max_sector_real_concentration_pct", "sector"),
            ("playbook_pct", "max_playbook_real_concentration_pct", "playbook"),
        )
        for field, cap_field, label in exposure_caps:
            value = optional_float(exposure.get(field))
            cap_pct = optional_float(risk.get(cap_field))
            if value is not None and cap_pct is not None and value > cap_pct:
                hard_triggers.append(f"{label} exposure {value:g}% exceeds {cap_pct:g}% cap")

    if action == "proceed" and hard_triggers:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="thesis.entry-preflight-proceed-trigger",
                message=(
                    "entry_preflight.action: proceed is not allowed with "
                    + ", ".join(hard_triggers)
                ),
                location="entry_preflight.action",
            )
        )
    return findings
