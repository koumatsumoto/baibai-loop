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


def production_rules_contract_hash(rules_json: str) -> str:
    """Return the exact method identity used by production run and select."""

    from ..metrics import (
        BARS_INPUT_WINDOW_DAYS,
        VALUATION_CALCULATION_REVISION,
        VALUATION_HISTORY_SESSIONS,
    )

    return rules_contract_hash(
        rules_json,
        valuation_calculation_revision=VALUATION_CALCULATION_REVISION,
        variant="production",
        valuation_history_sessions=VALUATION_HISTORY_SESSIONS,
        bars_input_window_days=BARS_INPUT_WINDOW_DAYS,
        production_authority=True,
    )
