"""出所ごとの受領履歴と比較結果。提供元の異なる尺度を統合scoreにしない。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from baibai_engine.machine_analysis.observations.external_assessments.history import (
    AnalystForecastHistory,
    AnalystRatingHistory,
    ModelValuationHistory,
    QuantsScoreHistory,
    TradingViewHistory,
)
from baibai_engine.machine_analysis.observations.external_assessments.models import FrozenModel

METHOD_VERSION = "same-series-v4"


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
    availability: Literal[
        "not_observed",
        "unresolved",
        "all_null",
        "forecast_fields_present",
        "excluded_by_source_terms",
    ]
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
    minkabu: AnalystForecastHistory = Field(default_factory=AnalystForecastHistory)
