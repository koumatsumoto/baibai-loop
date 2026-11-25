"""出所ごとの受領履歴と比較結果。提供元の異なる尺度を統合scoreにしない。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from baibai_engine.machine_analysis.observations.external_assessments.history import (
    AnalystForecastHistory,
    AnalystRatingHistory,
    ModelValuationHistory,
    QuantsScoreHistory,
    TradingViewHistory,
    latest_monex,
)
from baibai_engine.machine_analysis.observations.external_assessments.identity import (
    monex_public_url,
)
from baibai_engine.machine_analysis.observations.external_assessments.models import (
    FrozenModel,
    MonexEPSReceipt,
)

METHOD_VERSION = "same-series-v4"
SINGLE_CUTOFF_METHOD_VERSION = "same-series-v11-no-tradingview"
MONEX_DAY_PAIR_METHOD_VERSION = "same-series-v11-monex-asof-pair-no-tradingview"
_CURRENT_METHOD_VERSIONS = {SINGLE_CUTOFF_METHOD_VERSION, MONEX_DAY_PAIR_METHOD_VERSION}


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


class _TradingViewInputs(FrozenModel):
    inputs: TradingViewHistory
    availability: Literal[
        "not_observed",
        "unresolved",
        "all_null",
        "forecast_fields_present",
        "excluded_by_source_terms",
    ]
    forecast_fields: tuple[str, ...]


class TradingViewReceipts(_TradingViewInputs):
    comparison_basis: Literal["metric_period_mapping_unconfirmed"]


class SavedTradingViewComparisons(_TradingViewInputs):
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
    tradingview: TradingViewReceipts | SavedTradingViewComparisons | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    origin_groups: tuple[OriginGroup, ...]
    minkabu: AnalystForecastHistory = Field(default_factory=AnalystForecastHistory)
    monex_fiscal_eps: MonexEPSReceipt | None = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def fiscal_eps_matches_price_original(self) -> Self:
        if (self.tradingview is None) != (self.method_version in _CURRENT_METHOD_VERSIONS):
            raise ValueError("TradingView section differs from the external assessment revision")
        eps = self.monex_fiscal_eps
        if eps is None:
            return self
        anchor = latest_monex(item.inputs for item in self.model_valuations)
        if (
            self.method_version
            not in {
                "same-series-v9-monex-fiscal-eps",
                "same-series-v10-monex-asof-pair",
                *_CURRENT_METHOD_VERSIONS,
            }
            or anchor is None
            or (eps.ticker, anchor.ticker) != (self.ticker, self.ticker)
            or eps.source_url != monex_public_url(self.ticker)
            or anchor.source_url != eps.source_url
            or eps.received_at_utc.utcoffset() is None
            or self.cutoff.utcoffset() is None
            or self.evaluated_at.utcoffset() is None
            or anchor.fetched_at_utc != eps.received_at_utc
            or not eps.received_at_utc <= self.cutoff <= self.evaluated_at
            or eps.reference_eps is None
            or not any(
                row and row.eps_jpy_per_share is not None
                for row in (eps.reference_eps.current_fy, eps.reference_eps.next_fy)
            )
        ):
            raise ValueError("fiscal EPS differs from this company's fixed Monex price observation")
        return self
