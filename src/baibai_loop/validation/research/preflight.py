"""Entry-preflight checks applied to approved act-now decisions."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Literal

from baibai_loop.screening.regime import RALLY_RETURN_20D_MIN, SELLOFF_RETURN_20D_MAX
from baibai_loop.validation.domain import (
    as_list,
    as_mapping,
    number,
)
from baibai_loop.validation.errors import ValidationFinding

from .shared import _KNOWN_MACRO_CONTEXT_FRESHNESS, _gate_boundary_date, _research_record_date

_KNOWN_ENTRY_PREFLIGHT_ACTIONS = {"proceed", "starter", "defer", "exception"}
_KNOWN_ENTRY_PREFLIGHT_EXCEPTION_BASES = {
    "near_term_catalyst",
    "low_sizing",
    "low_correlation",
}
_ENTRY_PREFLIGHT_EFFECTIVE_DATE = date(2026, 6, 1)
_REGIME_GATE_EFFECTIVE_DATE = date(2026, 6, 17)
_KNOWN_MARKET_REGIMES = {
    "risk_on_rally",
    "risk_off_selloff",
    "neutral_range",
    "unknown",
}
# In a risk_on_rally the index trends up while anti-momentum value entries
# structurally lag it: the screening regime lens only neutralizes the
# fast-dislocation boost in ranking (lens, not gate), so the recommended book
# still trailed 1321 in every risk_on_rally week of 2026-05 (entry->eval cap
# 6/12: the contrarian book -1.4pt, the screen's own top5 -8.8pt;


# backtest-runbook §6 dated index: regime lens 4w -4pt; counterfactual with 8 approved
# entries showed cumulative rel -27.6pt that a defer rule would have avoided,
# bootstrap 95% CI [-7.60pt, +0.32pt]). near_term_catalyst supplies the
# mean-reversion trigger; low_correlation dilutes the regime bet; without
# either, defer. low_sizing alone does not waive: starter-size entries also
# lost in every 2026-05 rally week.
_RALLY_CONTRARIAN_WAIVER_BASES = {"low_correlation"}


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

    raw_market_regime = preflight.get("market_regime")
    market_regime: Mapping[str, object] = as_mapping(raw_market_regime)
    regime_label = market_regime.get("regime")
    benchmark_return_20d = number(market_regime.get("benchmark_return_20d"))
    regime_asof = market_regime.get("asof") or market_regime.get("eval_date")
    benchmark_ticker = market_regime.get("benchmark_ticker") or "1321"

    # Gate-date uses _gate_boundary_date (max of all signals) so a backdated
    # published_at on a file actually authored on/after 2026-06-17 cannot evade
    # the gate. Filename is the most tamper-resistant signal because the
    # YYYY-MM-DD naming convention is enforced elsewhere and git-reviewable.
    gate_date = _gate_boundary_date(front_matter, path=path) or research_date
    regime_gate_applies = gate_date >= _REGIME_GATE_EFFECTIVE_DATE

    market_regime_present = isinstance(raw_market_regime, Mapping)
    if regime_gate_applies and not (market_regime_present and isinstance(regime_label, str)):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.entry-preflight-regime-required",
                message=(
                    "approved research dated 2026-06-17 or later requires "
                    "entry_preflight.market_regime.regime — paste the output of "
                    "`uv run baibai-loop-screening ticker-profile --ticker <T> --asof <D>` "
                    "regime block, or run `uv run baibai-loop-screening bootstrap-cache` "
                    "if data/screening/market.sqlite is missing"
                ),
                location="entry_preflight.market_regime",
            )
        )
    if regime_label is not None and regime_label not in _KNOWN_MARKET_REGIMES:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="research.entry-preflight-regime-label",
                message=(
                    "entry_preflight.market_regime.regime must be risk_on_rally, "
                    "risk_off_selloff, neutral_range, or unknown"
                ),
                location="entry_preflight.market_regime.regime",
            )
        )

    # Cross-check: the declared regime label must match what classify_market_regime
    # would assign given benchmark_return_20d. Without this, an author could write
    # `regime: neutral_range, benchmark_return_20d: 0.10` and bypass the gate.
    if regime_label in {"risk_on_rally", "risk_off_selloff", "neutral_range"} and (
        benchmark_return_20d is not None
    ):
        if benchmark_return_20d >= RALLY_RETURN_20D_MIN:
            expected = "risk_on_rally"
        elif benchmark_return_20d <= SELLOFF_RETURN_20D_MAX:
            expected = "risk_off_selloff"
        else:
            expected = "neutral_range"
        if regime_label != expected:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="research.entry-preflight-regime-mismatch",
                    message=(
                        f"entry_preflight.market_regime.regime '{regime_label}' contradicts "
                        f"benchmark_return_20d={benchmark_return_20d:+.4f} "
                        f"(classify_market_regime would label it '{expected}')"
                    ),
                    location="entry_preflight.market_regime.regime",
                )
            )

    # A risk_on_rally is a hard trigger: a full-size proceed is disallowed because
    # the contrarian value entry mechanically lags a trending index. The decision
    # drops to starter/exception (smaller bet) or defer. `unknown` is also a hard
    # trigger because it gives an out by claiming "data unavailable"; the operator
    # must run bootstrap-cache first or choose defer.
    if regime_label == "risk_on_rally":
        hard_triggers.append("market regime risk_on_rally")
    if regime_label == "unknown" and regime_gate_applies:
        hard_triggers.append("market regime unknown (insufficient data; run bootstrap-cache)")

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

    # Discipline rule: even a starter-size contrarian entry trailed the index in
    # every 2026-05 risk_on_rally week. Unless a near-term catalyst or
    # low-correlation basis justifies it, defer. For action=exception this is
    # an error (the operator explicitly chose to override the gate with
    # exception_basis; low_sizing alone is insufficient because starter-size
    # also lost). For action=starter it is a warning (operator chose smaller
    # size; the nudge points to defer).
    near_term_catalyst = preflight.get("near_term_catalyst") is True
    if (
        regime_label == "risk_on_rally"
        and action in {"starter", "exception"}
        and not (near_term_catalyst)
    ):
        basis = as_list(preflight.get("exception_basis"))
        if not any(item in _RALLY_CONTRARIAN_WAIVER_BASES for item in basis):
            severity: Literal["error", "warning"] = "error" if action == "exception" else "warning"
            ret20_phrase = (
                f"benchmark_return_20d={benchmark_return_20d:+.4f} ({benchmark_ticker})"
                if benchmark_return_20d is not None
                else "benchmark_return_20d unavailable"
            )
            asof_phrase = f", asof {regime_asof}" if isinstance(regime_asof, str) else ""
            findings.append(
                ValidationFinding(
                    severity=severity,
                    target=path,
                    code="research.entry-preflight-rally-contrarian",
                    message=(
                        f"risk_on_rally regime ({ret20_phrase}{asof_phrase}) with no "
                        "near_term_catalyst and no low_correlation basis: a contrarian "
                        "value entry structurally lags a trending index. "
                        "Add `near_term_catalyst: true` with a dated event, "
                        "supply `exception_basis: [low_correlation]`, or "
                        "downgrade `action: defer`."
                    ),
                    location="entry_preflight.market_regime",
                )
            )
    return findings
