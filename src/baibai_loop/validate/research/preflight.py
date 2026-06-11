"""Entry-preflight checks applied to approved act-now decisions."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path

from baibai_loop.validate.domain import (
    as_list,
    as_mapping,
    number,
)
from baibai_loop.validate.errors import ValidationFinding

from .shared import _KNOWN_MACRO_CONTEXT_FRESHNESS, _research_record_date

_KNOWN_ENTRY_PREFLIGHT_ACTIONS = {"proceed", "starter", "defer", "exception"}
_KNOWN_ENTRY_PREFLIGHT_EXCEPTION_BASES = {
    "near_term_catalyst",
    "low_sizing",
    "low_correlation",
}
_ENTRY_PREFLIGHT_EFFECTIVE_DATE = date(2026, 6, 1)


def _check_entry_preflight(
    path: Path, front_matter: Mapping[str, object]
) -> list[ValidationFinding]:
    decision = front_matter.get("research_decision")
    outcome = decision.get("outcome") if isinstance(decision, Mapping) else None
    if outcome != "approved":
        return []
    research_date = _research_record_date(front_matter, path=path)
    if research_date is None or research_date < _ENTRY_PREFLIGHT_EFFECTIVE_DATE:
        return []

    preflight = front_matter.get("entry_preflight")
    if not isinstance(preflight, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="research.entry-preflight-required",
                message=("approved research dated 2026-06-01 or later requires entry_preflight"),
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
                code="research.entry-preflight-action",
                message=("entry_preflight.action must be proceed, starter, defer, or exception"),
                location="entry_preflight.action",
            )
        )
    reason = preflight.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.entry-preflight-reason",
                message="entry_preflight.reason is required",
                location="entry_preflight.reason",
            )
        )

    preflight_freshness = preflight.get("macro_freshness")
    macro_fit = as_mapping(front_matter.get("macro_context_fit"))
    macro_freshness = macro_fit.get("context_freshness")
    if preflight_freshness not in _KNOWN_MACRO_CONTEXT_FRESHNESS:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.entry-preflight-macro-freshness",
                message="entry_preflight.macro_freshness must be current, stale, or future",
                location="entry_preflight.macro_freshness",
            )
        )
    elif isinstance(macro_freshness, str) and preflight_freshness != macro_freshness:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.entry-preflight-macro-freshness-match",
                message=(
                    "entry_preflight.macro_freshness must match macro_context_fit.context_freshness"
                ),
                location="entry_preflight.macro_freshness",
            )
        )

    market_relative = number(preflight.get("market_relative_return_pct"))
    sector_relative = number(preflight.get("sector_or_peer_relative_return_pct"))
    exposure = as_mapping(preflight.get("tactical_exposure_after_order"))
    sector_exposure = number(exposure.get("sector_33_pct"))
    playbook_exposure = number(exposure.get("playbook_pct"))

    hard_triggers: list[str] = []
    if preflight_freshness == "future":
        hard_triggers.append("future macro freshness")
    if preflight_freshness == "stale":
        hard_triggers.append("stale macro freshness")
    if market_relative is not None and market_relative <= -3:
        hard_triggers.append("market relative return <= -3pt")
    if sector_relative is not None and sector_relative <= -3:
        hard_triggers.append("sector/peer relative return <= -3pt")
    if sector_exposure is not None and sector_exposure > 50:
        hard_triggers.append("sector exposure > 50% tactical budget")
    if playbook_exposure is not None and playbook_exposure > 50:
        hard_triggers.append("playbook exposure > 50% tactical budget")

    if preflight_freshness == "future":
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.entry-preflight-future-approved",
                message="approved research cannot use future macro freshness in entry_preflight",
                location="entry_preflight.macro_freshness",
            )
        )
    if action == "proceed" and hard_triggers:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.entry-preflight-proceed-trigger",
                message=(
                    "entry_preflight.action: proceed is not allowed with "
                    + ", ".join(hard_triggers)
                ),
                location="entry_preflight.action",
            )
        )
    if action == "exception":
        basis = as_list(preflight.get("exception_basis"))
        known_basis = [item for item in basis if item in _KNOWN_ENTRY_PREFLIGHT_EXCEPTION_BASES]
        if len(known_basis) != len(basis) or not known_basis:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.entry-preflight-exception-basis",
                    message=(
                        "entry_preflight.action: exception requires exception_basis "
                        "from near_term_catalyst, low_sizing, or low_correlation"
                    ),
                    location="entry_preflight.exception_basis",
                )
            )
        if "near_term_catalyst" in known_basis and preflight.get("near_term_catalyst") is not True:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.entry-preflight-exception-basis",
                    message=(
                        "entry_preflight.exception_basis: near_term_catalyst requires "
                        "near_term_catalyst: true"
                    ),
                    location="entry_preflight.near_term_catalyst",
                )
            )
        if "low_sizing" in known_basis and (
            sector_exposure is None
            or playbook_exposure is None
            or sector_exposure > 25
            or playbook_exposure > 25
        ):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.entry-preflight-exception-basis",
                    message=(
                        "entry_preflight.exception_basis: low_sizing requires sector "
                        "and playbook exposure after order <= 25%"
                    ),
                    location="entry_preflight.exception_basis",
                )
            )
    return findings
