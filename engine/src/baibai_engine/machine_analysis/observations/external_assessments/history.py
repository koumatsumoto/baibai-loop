"""保存観測を同じcutoffで束ねる履歴入力。比較や評価はモデル側が所有する。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import AwareDatetime, Field, FiniteFloat

from baibai_engine.machine_analysis.observations.external_assessments.models import (
    AnalystForecastObservation,
    AnalystRatingObservation,
    FrozenModel,
    ModelValuationObservation,
    QuantsScoreObservation,
)


class ModelValuationHistory(FrozenModel):
    latest: ModelValuationObservation | None = None
    previous: ModelValuationObservation | None = None


class AnalystRatingHistory(FrozenModel):
    latest: AnalystRatingObservation | None = None
    previous: AnalystRatingObservation | None = None


class QuantsScoreHistory(FrozenModel):
    latest: QuantsScoreObservation | None = None
    previous: QuantsScoreObservation | None = None


class TradingViewObservation(FrozenModel):
    snapshot_date: date
    ticker: str
    provider_symbol: str
    fetched_at_utc: AwareDatetime
    fetch_status: Literal["ok", "unresolved"]
    quote_at_utc: AwareDatetime | None = None
    estimate_currency: str | None = None
    estimate_unit: str | None = None
    provider_updated_at_utc: AwareDatetime | None = None
    target_period_label: str | None = None
    target_period_end: str | None = None
    analyst_rating: str | None = None
    recommendation_total: int | None = None
    price_target_average: FiniteFloat | None = None
    price_target_high: FiniteFloat | None = None
    price_target_low: FiniteFloat | None = None
    eps_forecast_next_fq: FiniteFloat | None = None
    eps_forecast_next_fy: FiniteFloat | None = None
    revenue_forecast_next_fq: FiniteFloat | None = None
    revenue_forecast_next_fy: FiniteFloat | None = None
    forward_pe: FiniteFloat | None = None
    forward_ps: FiniteFloat | None = None
    forward_ev_ebitda: FiniteFloat | None = None
    ebitda_estimate_fy: FiniteFloat | None = None
    free_cash_flow_estimate_fy: FiniteFloat | None = None
    capex_estimate_fy: FiniteFloat | None = None
    total_debt_estimate_fy: FiniteFloat | None = None
    quote_close: FiniteFloat | None = None
    quote_currency: str | None = None


class TradingViewHistory(FrozenModel):
    latest: TradingViewObservation | None = None
    previous: TradingViewObservation | None = None


class AnalystForecastHistory(FrozenModel):
    latest: AnalystForecastObservation | None = None
    previous: AnalystForecastObservation | None = None


class ObservationHistory(FrozenModel):
    ticker: str
    cutoff: datetime
    model_valuations: tuple[ModelValuationHistory, ...] = ()
    analyst_ratings: tuple[AnalystRatingHistory, ...] = ()
    quants_scores: tuple[QuantsScoreHistory, ...] = ()
    tradingview: TradingViewHistory = Field(default_factory=TradingViewHistory)
    minkabu: AnalystForecastHistory = Field(default_factory=AnalystForecastHistory)
