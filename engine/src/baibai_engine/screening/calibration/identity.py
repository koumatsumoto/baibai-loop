"""Lightweight semantic identity for calibration and current screening rules."""

from __future__ import annotations

from hashlib import sha256


def rules_contract_hash(
    rules_json: str,
    *,
    valuation_calculation_revision: str,
    variant: str,
    valuation_history_sessions: int,
    bars_input_window_days: int,
    production_authority: bool,
) -> str:
    contract = (
        f"{rules_json}|{valuation_calculation_revision}|{variant}|{valuation_history_sessions}|"
        f"{bars_input_window_days}|{production_authority}"
    )
    return sha256(contract.encode("utf-8")).hexdigest()[:16]
