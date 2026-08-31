"""Lightweight semantic identity for calibration and current screening rules."""

from __future__ import annotations

from hashlib import sha256


def rules_contract_hash(
    rules_json: str,
    *,
    candidate_discovery_method_hash: str = "",
    valuation_calculation_revision: str,
    variant: str,
    valuation_history_sessions: int,
    bars_input_window_days: int,
    production_authority: bool,
) -> str:
    # Production run identity predates Candidate Discovery replay fidelity. Keep
    # the exact old byte contract when callers do not provide that calibration-only
    # identity, so a panel improvement cannot invalidate serving artifacts.
    if candidate_discovery_method_hash:
        contract = (
            f"{rules_json}|{candidate_discovery_method_hash}|{valuation_calculation_revision}|"
            f"{variant}|{valuation_history_sessions}|{bars_input_window_days}|"
            f"{production_authority}"
        )
    else:
        contract = (
            f"{rules_json}|{valuation_calculation_revision}|{variant}|"
            f"{valuation_history_sessions}|{bars_input_window_days}|{production_authority}"
        )
    return sha256(contract.encode("utf-8")).hexdigest()[:16]
