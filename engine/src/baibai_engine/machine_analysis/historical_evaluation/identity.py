"""Semantic identity for legacy-run rules and their calibration replay."""

from __future__ import annotations

from hashlib import sha256


def rules_contract_hash(
    rules_json: str,
    *,
    candidate_discovery_method_hash: str = "",
    universe_scope_revision: str = "",
    valuation_calculation_revision: str,
    market_context_calculation_revision: str,
    variant: str,
    valuation_history_sessions: int,
    bars_input_window_days: int,
    production_authority: bool,
) -> str:
    # Replay identity includes the candidate method; both paths include domain
    # calculation revisions. Retained artifacts keep their stored identifiers.
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
    if universe_scope_revision:
        contract = f"{contract}|{universe_scope_revision}"
    contract = f"{contract}|{market_context_calculation_revision}"
    return sha256(contract.encode("utf-8")).hexdigest()[:16]
