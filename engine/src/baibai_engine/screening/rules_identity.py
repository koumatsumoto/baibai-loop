"""Semantic identity shared by production screening producers and validators.

This module stays outside ``calibration.identity`` because calibration panels import
that lower-level hash primitive. Production-only helpers cannot change historical
panel rows and therefore must not force a full calibration rebuild.
"""

from __future__ import annotations

from .calibration.identity import rules_contract_hash
from .universe import UNIVERSE_SCOPE_REVISION


def production_rules_contract_hash(
    rules_json: str, *, universe_scope_revision: str = UNIVERSE_SCOPE_REVISION
) -> str:
    """Return the exact screening identity used by production runs and candidate contexts."""

    from baibai_engine.market_context.margin import MARKET_CONTEXT_CALCULATION_REVISION
    from baibai_engine.screening.metrics.snapshot import BARS_INPUT_WINDOW_DAYS
    from baibai_engine.valuation.metrics.financial_snapshot import VALUATION_CALCULATION_REVISION
    from baibai_engine.valuation.metrics.history import VALUATION_HISTORY_SESSIONS

    return rules_contract_hash(
        rules_json,
        universe_scope_revision=universe_scope_revision,
        valuation_calculation_revision=VALUATION_CALCULATION_REVISION,
        market_context_calculation_revision=MARKET_CONTEXT_CALCULATION_REVISION,
        variant="production",
        valuation_history_sessions=VALUATION_HISTORY_SESSIONS,
        bars_input_window_days=BARS_INPUT_WINDOW_DAYS,
        production_authority=True,
    )
