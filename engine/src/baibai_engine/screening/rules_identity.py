"""Semantic identity shared by production screening producers and validators.

This module stays outside ``calibration.identity`` because calibration panels import
that lower-level hash primitive. Production-only helpers cannot change historical
panel rows and therefore must not force a full calibration rebuild.
"""

from __future__ import annotations

from .calibration.identity import rules_contract_hash


def production_rules_contract_hash(rules_json: str) -> str:
    """Return the exact method identity used by production run and select."""

    from .metrics import (
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
