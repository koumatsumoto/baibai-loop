"""出所ごとの受領履歴と比較結果。提供元の異なる尺度を統合scoreにしない。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import AwareDatetime, Field, FiniteFloat

from baibai_engine.external_assessment.observations.models import (
    AnalystRatingObservation,
    FrozenModel,
    ModelValuationObservation,
    QuantsScoreObservation,
)

METHOD_VERSION = "same-series-v3"


class ModelValuationHistory(FrozenModel):
    latest: ModelValuationObservation | None = None
    previous: ModelValuationObservation | None = None


class AnalystRatingHistory(FrozenModel):
    latest: AnalystRatingObservation | None = None
    previous: AnalystRatingObservation | None = None


class QuantsScoreHistory(FrozenModel):
    latest: QuantsScoreObservation | None = None
    previous: QuantsScoreObservation | None = None


class MetricComparison(FrozenModel):
    metric: str
    state: Literal[
        "not_observed",
        "first_observation",
        "missing",
        "recovered",
        "count_changed",
        "not_comparable",
        "same_value",
        "increased",
        "decreased",
    ]
    comparable: bool = False
    reason: str | None = None
    latest_value: float | None = None
    previous_value: float | None = None
    latest_count: int | None = None
    previous_count: int | None = None
    delta: float | None = None


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


class ObservationHistory(FrozenModel):
    ticker: str
    cutoff: datetime
    model_valuations: tuple[ModelValuationHistory, ...] = ()
    analyst_ratings: tuple[AnalystRatingHistory, ...] = ()
    quants_scores: tuple[QuantsScoreHistory, ...] = ()
    tradingview: TradingViewHistory = Field(default_factory=TradingViewHistory)


class ModelValuationAssessment(FrozenModel):
    inputs: ModelValuationHistory
    comparisons: tuple[MetricComparison, ...]


class AnalystRatingAssessment(FrozenModel):
    distributor: str
    inputs: AnalystRatingHistory
    comparison: MetricComparison


class QuantsScoreAssessment(FrozenModel):
    inputs: QuantsScoreHistory
    comparisons: tuple[MetricComparison, ...]


class TradingViewAssessment(FrozenModel):
    inputs: TradingViewHistory
    availability: Literal["not_observed", "unresolved", "all_null", "forecast_fields_present"]
    forecast_fields: tuple[str, ...]
    comparisons: tuple[MetricComparison, ...]


class OriginReference(FrozenModel):
    distributor: str
    kind: str
    metric: str
    fetched_at_utc: datetime
    source_url: str


class OriginGroup(FrozenModel):
    origin: str
    references: tuple[OriginReference, ...]


class ExternalAssessment(FrozenModel):
    ticker: str
    cutoff: datetime
    evaluated_at: datetime
    method_version: str = METHOD_VERSION
    model_valuations: tuple[ModelValuationAssessment, ...]
    analyst_ratings: tuple[AnalystRatingAssessment, ...]
    quants_scores: tuple[QuantsScoreAssessment, ...]
    tradingview: TradingViewAssessment
    origin_groups: tuple[OriginGroup, ...]
