"""旧runと較正文脈の意味identity。新しい五モデル・候補政策のidentityではない。"""

from __future__ import annotations

from baibai_engine.machine_analysis.historical_evaluation.identity import rules_contract_hash
from .universe import UNIVERSE_SCOPE_REVISION


def production_rules_contract_hash(
    rules_json: str, *, universe_scope_revision: str = UNIVERSE_SCOPE_REVISION
) -> str:
    """Keep the historical production-variant identity used by calibration contexts."""

    from baibai_engine.machine_analysis.models.company_financials.metrics.financial_snapshot import (
        VALUATION_CALCULATION_REVISION,
    )
    from baibai_engine.machine_analysis.models.company_financials.metrics.history import (
        VALUATION_HISTORY_SESSIONS,
    )
    from baibai_engine.machine_analysis.models.market_context.margin import (
        MARKET_CONTEXT_CALCULATION_REVISION,
    )
    from baibai_engine.machine_analysis.historical_evaluation.input_windows import BARS_INPUT_WINDOW_DAYS

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
