"""実績利益の選択とaccruals。複数FYの較正signalは歴史評価が所有する。"""

from __future__ import annotations

from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.financials.metric_quality import (
    OperatingProfitSource,
)


def _select_operating_profit(
    summary: JQuantsFinancialSummary | None,
) -> tuple[float | None, OperatingProfitSource]:
    if summary is None:
        return None, OperatingProfitSource.NULL
    if summary.operating_profit is not None:
        return summary.operating_profit, OperatingProfitSource.OPERATING_PROFIT
    if summary.ordinary_profit is not None:
        return summary.ordinary_profit, OperatingProfitSource.ORDINARY_PROFIT
    if summary.profit is not None:
        return summary.profit, OperatingProfitSource.PROFIT
    return None, OperatingProfitSource.NULL


def _accruals_to_assets(
    *,
    net_income: float | None,
    ocf_ttm: float | None,
    total_assets: float | None,
    prior_total_assets: float | None,
) -> float | None:
    """Sloan (1996) accruals ratio: (NI - CFO) / average total assets.

    NI is the reported profit line composed to TTM, not a product of a per-share
    figure and a share count: EPS is per average share excluding treasury while the
    issued count includes it, so the product describes neither. The denominator uses
    the average of current and prior-year total assets when both are present,
    otherwise the current value. Returns None when NI, CFO, or current total assets
    is missing, or when the resulting denominator is nonpositive.
    """
    if net_income is None or ocf_ttm is None or total_assets is None:
        return None
    denominator = (
        (total_assets + prior_total_assets) / 2.0
        if prior_total_assets is not None and prior_total_assets > 0
        else total_assets
    )
    if denominator <= 0:
        return None
    return (net_income - ocf_ttm) / denominator


def _loss_narrowing(current: float | None, previous: float | None) -> bool | None:
    if current is None or previous is None:
        return None
    if current >= 0:
        return False
    return previous < 0 and current > previous
