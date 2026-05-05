from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from enum import StrEnum
from math import isfinite
from typing import Annotated, Any

from pydantic import ConfigDict, Field, field_validator, model_validator
from pydantic.dataclasses import dataclass

_MODEL_CONFIG = ConfigDict(
    strict=True,
    arbitrary_types_allowed=False,
    validate_assignment=False,
)
_TICKER_PATTERN = r"^[0-9A-Z]{4}$"

type NullableFloatMap = Mapping[str, float | None]
type MetricBreakdown = Mapping[str, NullableFloatMap]
type MetricValueMap = Mapping[str, float | int | bool | str | None]
type Ticker = Annotated[str, Field(pattern=_TICKER_PATTERN)]
type NonEmptyString = Annotated[str, Field(min_length=1)]
type NonNegativeInt = Annotated[int, Field(ge=0)]


def normalize_ticker(value: str) -> str:
    ticker = value.strip().upper()
    if len(ticker) != 4 or not ticker.isalnum():
        raise ValueError(f"ticker must be a 4-character alphanumeric string: {value!r}")
    return ticker


class TTMQuality(StrEnum):
    EXACT = "exact"
    APPROXIMATED = "approximated"
    UNAVAILABLE = "unavailable"


class OperatingProfitSource(StrEnum):
    OPERATING_PROFIT = "OperatingProfit"
    ORDINARY_PROFIT = "OrdinaryProfit"
    PROFIT = "Profit"
    NULL = "null"


def _validate_finite(value: float | None) -> float | None:
    if value is not None and not isfinite(value):
        raise ValueError("numeric values must be finite")
    return value


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class SecurityMaster:
    code: Ticker
    name: NonEmptyString
    market_segment: NonEmptyString
    sector_33: NonEmptyString
    is_common_stock: bool

    @field_validator("code", mode="before")
    @classmethod
    def _normalize_code(cls, value: str) -> str:
        return normalize_ticker(value)


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class UniverseSnapshot:
    market_cap_oku: int | None
    avg_turnover_oku: float | None
    exclusion_flags: tuple[str, ...] = ()

    @field_validator("exclusion_flags", mode="before")
    @classmethod
    def _tuple_exclusion_flags(cls, value: Sequence[str]) -> tuple[str, ...]:
        return tuple(value)

    @field_validator("avg_turnover_oku")
    @classmethod
    def _finite_turnover(cls, value: float | None) -> float | None:
        return _validate_finite(value)


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class FinancialSnapshot:
    per_forward: float | None
    per_trailing: float | None
    pbr: float | None
    ev_ebitda: float | None
    p_s: float | None
    pcfr: float | None
    eps: float | None
    sales_ttm: float | None
    ocf_ttm: float | None
    edinet_ocf_ttm: float | None = None
    sales: float | None = None
    cfo: float | None = None
    cash_eq: float | None = None
    total_assets: float | None = None
    equity: float | None = None
    market_cap: float | None = None
    cash_to_market_cap: float | None = None
    price_to_equity: float | None = None
    equity_ratio: float | None = None
    ocf_yield: float | None = None
    net_cash: float | None = None
    net_cash_to_market_cap: float | None = None
    fcf_ttm: float | None = None
    fcf_yield: float | None = None
    capex_ttm: float | None = None
    depreciation_and_amortization_ttm: float | None = None
    debt: float | None = None
    cash: float | None = None
    ebitda_ttm: float | None = None
    consolidation_basis: str | None = None
    edinet_source_doc_id: str | None = None
    edinet_document_type: str | None = None
    edinet_source_submit_datetime: str | None = None
    edinet_source_period_start: date | None = None
    edinet_source_period_end: date | None = None
    edinet_capex_source: str | None = None
    edinet_failure_reasons: str | None = None
    operating_profit: float | None = None
    operating_profit_source: OperatingProfitSource = OperatingProfitSource.NULL
    eps_yoy: float | None = None
    sales_yoy: float | None = None
    operating_profit_yoy: float | None = None
    cfo_yoy: float | None = None
    operating_profit_loss_narrowing: bool | None = None
    ttm_quality_ev_ebitda: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_p_s: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_pcfr: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_ocf_yield: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_sales: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_fcf_yield: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_net_cash: TTMQuality = TTMQuality.UNAVAILABLE
    shares_outstanding: float | None = None

    @field_validator(
        "per_forward",
        "per_trailing",
        "pbr",
        "ev_ebitda",
        "p_s",
        "pcfr",
        "eps",
        "sales_ttm",
        "ocf_ttm",
        "edinet_ocf_ttm",
        "sales",
        "cfo",
        "cash_eq",
        "total_assets",
        "equity",
        "market_cap",
        "cash_to_market_cap",
        "price_to_equity",
        "equity_ratio",
        "ocf_yield",
        "net_cash",
        "net_cash_to_market_cap",
        "fcf_ttm",
        "fcf_yield",
        "capex_ttm",
        "depreciation_and_amortization_ttm",
        "debt",
        "cash",
        "ebitda_ttm",
        "operating_profit",
        "eps_yoy",
        "sales_yoy",
        "operating_profit_yoy",
        "cfo_yoy",
        "shares_outstanding",
    )
    @classmethod
    def _finite_numeric_fields(cls, value: float | None) -> float | None:
        return _validate_finite(value)


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class DerivedMetrics:
    price_change_60d: float | None
    sector_relative_strength_4w: float | None
    sector_median_gap: NullableFloatMap = Field(default_factory=dict)
    self_range_percentile: NullableFloatMap = Field(default_factory=dict)
    sigma_gap: NullableFloatMap = Field(default_factory=dict)
    sector_relative_strength_percentile: float | None = None
    ticker_return_4w: float | None = None
    sector_return_4w: float | None = None
    short_history_flag: bool = False
    split_adjustment_flag: bool = False

    @field_validator(
        "price_change_60d",
        "sector_relative_strength_4w",
        "sector_relative_strength_percentile",
        "ticker_return_4w",
        "sector_return_4w",
    )
    @classmethod
    def _finite_optional_float(cls, value: float | None) -> float | None:
        return _validate_finite(value)


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class SignalHit:
    name: NonEmptyString
    playbook: NonEmptyString
    reasons: tuple[str, ...]
    metrics: MetricValueMap = Field(default_factory=dict)

    @field_validator("reasons", mode="before")
    @classmethod
    def _tuple_reasons(cls, value: Sequence[str]) -> tuple[str, ...]:
        return tuple(value)


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class FreshnessWarning:
    source_family: NonEmptyString
    stale_metric: NonEmptyString
    reason: NonEmptyString
    event_date: date
    event_kind: NonEmptyString
    event_title: NonEmptyString
    event_source: NonEmptyString
    edinet_source_submit_datetime: str | None = None
    event_url: str | None = None


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class ScreeningResult:
    pass_fail: bool
    signals: tuple[SignalHit, ...] = ()
    failure_reasons: tuple[str, ...] = ()
    null_reasons: tuple[str, ...] = ()

    @field_validator("signals", "failure_reasons", "null_reasons", mode="before")
    @classmethod
    def _tuple_sequence(cls, value: Sequence[Any]) -> tuple[Any, ...]:
        return tuple(value)

    @model_validator(mode="after")
    def _consistent_result(self) -> ScreeningResult:
        if self.pass_fail and not self.signals:
            raise ValueError("pass_fail=True requires at least one signal")
        if not self.pass_fail and not self.failure_reasons:
            raise ValueError("pass_fail=False requires at least one failure_reasons")
        return self


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class ScreenedCandidate:
    ticker: Ticker
    name: NonEmptyString
    per_forward: float | None
    per_trailing: float | None
    pbr: float | None
    ev_ebitda: float | None
    p_s: float | None
    pcfr: float | None
    sector_33: NonEmptyString
    signals: tuple[SignalHit, ...]
    ttm_quality: Mapping[str, TTMQuality]
    market_cap_oku: int | None = None
    avg_turnover_oku: float | None = None
    price_change_60d: float | None = None
    price_change_4w: float | None = None
    sector_relative_strength_percentile: float | None = None
    metrics: MetricValueMap = Field(default_factory=dict)
    metrics_breakdown: MetricBreakdown = Field(default_factory=dict)
    next_earnings_date: date | None = None
    split_adjustment_flag: bool = False
    freshness_warnings: tuple[FreshnessWarning, ...] = ()

    @field_validator("signals", "freshness_warnings", mode="before")
    @classmethod
    def _tuple_sequence(cls, value: Sequence[Any]) -> tuple[Any, ...]:
        return tuple(value)

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker_field(cls, value: str) -> str:
        return normalize_ticker(value)

    @field_validator(
        "per_forward",
        "per_trailing",
        "pbr",
        "ev_ebitda",
        "p_s",
        "pcfr",
        "avg_turnover_oku",
        "price_change_60d",
        "price_change_4w",
        "sector_relative_strength_percentile",
    )
    @classmethod
    def _finite_optional_float(cls, value: float | None) -> float | None:
        return _validate_finite(value)


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class ScreenedRunDocument:
    run_date: date
    asof_date: date
    universe_size: NonNegativeInt
    filters: Mapping[str, Any]
    candidates: tuple[ScreenedCandidate, ...]
    run_at: datetime
    run_id: NonEmptyString
    config_hash: NonEmptyString
    cache_manifest_hash: NonEmptyString
    generated_by: str = "screening-cli-v1"
    data_sources: tuple[str, ...] = (
        "j-quants-light",
        "jpx-public-regulation",
    )
    fact_memo_lines: tuple[str, ...] = ()
    provider_status_lines: tuple[str, ...] = ()
    universe_exclusion_lines: tuple[str, ...] = ()
    ttm_quality_counts: Mapping[str, int] = Field(default_factory=dict)
    signals_summary: Mapping[str, int] = Field(default_factory=dict)
    fallback_lines: tuple[str, ...] = ()

    @field_validator(
        "candidates",
        "data_sources",
        "fact_memo_lines",
        "provider_status_lines",
        "universe_exclusion_lines",
        "fallback_lines",
        mode="before",
    )
    @classmethod
    def _tuple_sequence(cls, value: Sequence[Any]) -> tuple[Any, ...]:
        return tuple(value)

    @model_validator(mode="after")
    def _consistent_dates(self) -> ScreenedRunDocument:
        if self.run_date != self.asof_date:
            raise ValueError("run_date must equal asof_date")
        if self.run_at.tzinfo is None:
            raise ValueError("run_at must be timezone-aware")
        return self
