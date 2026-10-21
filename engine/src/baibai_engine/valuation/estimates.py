"""倍率・自己価格レンジとcarryによる機械期待収益推計。

意味・単位・入力basisは docs/reference/valuation-metrics.md が所有する。
候補選抜と企業固有の調査 Projection は扱わない。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from statistics import fmean
from typing import Literal

# 現在の MODEL_V1 policy parameters。実証的な変更は long-horizon authority を満たす
# artifact と人間レビューを経て code で明示的に変更し、自動更新はしない。
# Researchの条件付き評価・購入要求率とは別の推計仮定である。
# docs/reference/estimate-calibration.md#estimator-vs-decision
REALIZATION_RATE_ANNUAL = 0.10
UPSIDE_CAP = 0.50
SHARE_COUNT_CLIP = 0.05
EXPECTED_RETURN_MODEL_VERSION = "expected-return-v1"
EXPECTED_RETURN_UNIT = "annual_ratio"

# anchor に使う倍率軸。資産 (pbr) と収益 (per_forward → per_trailing fallback) の
# 2 系統を blend する。ただし両軸とも自己レンジ側で binding した銘柄では 2 つの upside が
# 同じ値になり (自己レンジは価格の履歴なので軸に依存しない)、平均はノイズを薄めない。
# as-of 2026-03-31 の実測では、E[r] を持つ 3,773 銘柄のうち 1,434 (38.0%) がこの形。
_EARNINGS_METRICS = ("per_forward", "per_trailing")


@dataclass(frozen=True, slots=True, kw_only=True)
class ExpectedReturnEstimate:
    """E[r]の年率成分、期間年率化前のupside比率、円建て価格anchor。"""

    er_annual: float
    reversion_annual: float
    carry_annual: float
    upside_blend: float
    upside_capped: float
    anchor_metrics: str
    dividend_yield: float | None
    share_count_yield: float | None
    fv_sector_median_yen: float | None
    fv_self_range_yen: float | None
    origin: Literal["estimate"] = "estimate"
    model_version: str = EXPECTED_RETURN_MODEL_VERSION
    unit: str = EXPECTED_RETURN_UNIT
    assumptions: str = (
        "reversion=0.10*clip(implied_upside,+/-0.50); "
        "carry=dividend_yield(forecast_preferred_unless_gt_2x_actual,split_safe)"
        "+clip(share_count_yield,+/-0.05)"
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ExpectedReturnInputs:
    """倍率・比較 anchor・carry と、その価格/株式基準だけを受け取る。"""

    pbr: float | None
    per_forward: float | None
    per_trailing: float | None
    sector_median_value: Mapping[str, float | None]
    self_range_median: Mapping[str, float | None]
    dividend_yield: float | None
    net_share_change_yoy: float | None
    dividend_basis_resolved: bool
    close: float | None


def estimate_expected_return(inputs: ExpectedReturnInputs) -> ExpectedReturnEstimate | None:
    """Estimate annual return and price anchors from available valuation axes.

    No usable anchor or an unresolved dividend basis produces no estimate.
    Ordinary missing carry components contribute zero by convention, not as an
    observation of zero payouts or unchanged shares; readers expose that assumption.
    """
    if not inputs.dividend_basis_resolved:
        return None

    upsides: dict[str, float] = {}
    sector_ratios: list[float] = []
    self_ratios: list[float] = []

    for metric in _anchor_metrics(inputs):
        current = _positive(getattr(inputs, metric))
        if current is None:
            continue
        sector_median = _positive(inputs.sector_median_value.get(metric))
        self_median = _positive(inputs.self_range_median.get(metric))
        anchors = [value for value in (sector_median, self_median) if value is not None]
        if not anchors:
            continue
        conservative_anchor = min(anchors)
        upsides[metric] = conservative_anchor / current - 1
        if sector_median is not None:
            sector_ratios.append(sector_median / current)
        if self_median is not None:
            self_ratios.append(self_median / current)

    if not upsides:
        return None

    upside_blend = fmean(upsides.values())
    if not isfinite(upside_blend):
        return None
    upside_capped = _clip(upside_blend, UPSIDE_CAP)
    reversion_annual = REALIZATION_RATE_ANNUAL * upside_capped

    dividend_yield = inputs.dividend_yield
    share_count_yield = (
        -inputs.net_share_change_yoy if inputs.net_share_change_yoy is not None else None
    )
    carry_annual = (dividend_yield or 0.0) + _clip(share_count_yield or 0.0, SHARE_COUNT_CLIP)

    return ExpectedReturnEstimate(
        er_annual=reversion_annual + carry_annual,
        reversion_annual=reversion_annual,
        carry_annual=carry_annual,
        upside_blend=upside_blend,
        upside_capped=upside_capped,
        anchor_metrics=",".join(sorted(upsides)),
        dividend_yield=dividend_yield,
        share_count_yield=share_count_yield,
        fv_sector_median_yen=(
            inputs.close * fmean(sector_ratios)
            if inputs.close is not None and sector_ratios
            else None
        ),
        fv_self_range_yen=(
            inputs.close * fmean(self_ratios) if inputs.close is not None and self_ratios else None
        ),
    )


def _anchor_metrics(inputs: ExpectedReturnInputs) -> tuple[str, ...]:
    """資産 anchor (pbr) + 収益 anchor (forward 優先、無ければ trailing)。"""
    earnings = next(
        (metric for metric in _EARNINGS_METRICS if _positive(getattr(inputs, metric))),
        None,
    )
    return ("pbr", earnings) if earnings else ("pbr",)


def _positive(value: float | None) -> float | None:
    return value if value is not None and value > 0 else None


def _clip(value: float, bound: float) -> float:
    return max(-bound, min(bound, value))
