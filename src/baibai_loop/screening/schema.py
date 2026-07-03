from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from enum import StrEnum
from math import isfinite
from typing import Annotated, Any

from pydantic import ConfigDict, Field, field_validator, model_validator
from pydantic.dataclasses import dataclass

from baibai_loop.market.ticker import normalize_ticker as normalize_ticker

_MODEL_CONFIG = ConfigDict(
    strict=True,
    arbitrary_types_allowed=False,
    validate_assignment=False,
)
_TICKER_PATTERN = r"^[0-9A-Z]{4}$"

type NullableFloatMap = Mapping[str, float | None]
type MetricValueMap = Mapping[str, float | int | bool | str | None]
type Ticker = Annotated[str, Field(pattern=_TICKER_PATTERN)]
type NonEmptyString = Annotated[str, Field(min_length=1)]
type NonNegativeInt = Annotated[int, Field(ge=0)]


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
    listing_span_days: int | None = None
    jpx_flags: tuple[str, ...] = ()

    @field_validator("jpx_flags", mode="before")
    @classmethod
    def _tuple_jpx_flags(cls, value: Sequence[str]) -> tuple[str, ...]:
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
    # 直近実績の年間 DPS (asof-basis 正規化済み)・進行期の予想年間 DPS・
    # 実績配当利回り (dps_actual_annual / 直近終値)。
    dps_actual_annual: float | None = None
    dps_forecast_annual: float | None = None
    dividend_yield: float | None = None
    # BS 系 fact (bps / cash_eq / equity / total_assets) の carry-forward 記録。
    # fields = latest 行に無く過去行から引いた field 名 (comma 区切り)、
    # lag_days = その最大遅延日数 (staleness fact)。
    bs_carry_forward_fields: str | None = None
    bs_carry_forward_lag_days: int | None = None
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
    # ttm_quality_* は「TTM 値の合成の質」であって値の有無ではない。分母が負・ゼロで
    # 比率 (per_trailing / pcfr 等) が None でも、合成に成功していれば exact のまま。
    ttm_quality_ev_ebitda: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_per_trailing: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_p_s: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_pcfr: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_ocf_yield: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_sales: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_fcf_yield: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_net_cash: TTMQuality = TTMQuality.UNAVAILABLE
    shares_outstanding: float | None = None
    # D2 accruals = (eps_ttm * shares - cfo_ttm) / average total assets — Sloan
    # 1996. High positive accruals are an earnings-quality flag (reported NI
    # not converting to cash). None if any input is missing.
    accruals_to_assets: float | None = None
    # D3 net share issuance YoY = (shares_now - shares_prior_year) / shares_prior_year.
    # Positive = dilution, negative = buyback. None if prior-year share count
    # is missing or zero.
    net_share_change_yoy: float | None = None

    @field_validator(
        "per_forward",
        "per_trailing",
        "pbr",
        "ev_ebitda",
        "p_s",
        "pcfr",
        "eps",
        "dps_actual_annual",
        "dps_forecast_annual",
        "dividend_yield",
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
        "accruals_to_assets",
        "net_share_change_yoy",
    )
    @classmethod
    def _finite_numeric_fields(cls, value: float | None) -> float | None:
        return _validate_finite(value)


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class DerivedMetrics:
    price_change_1d: float | None = None
    price_change_5d: float | None = None
    price_change_20d: float | None = None
    price_change_60d: float | None = None
    gap_from_52w_low: float | None = None
    turnover_spike_5d: float | None = None
    sector_relative_strength_4w: float | None = None
    sector_median_gap: NullableFloatMap = Field(default_factory=dict)
    # sector 中央値倍率の絶対値と自己レンジ (750 営業日) の中央値倍率。
    # 機械 E[r] / FV アンカーの入力 (gap / percentile と違い水準そのもの)。
    sector_median_value: NullableFloatMap = Field(default_factory=dict)
    self_range_percentile: NullableFloatMap = Field(default_factory=dict)
    self_range_median: NullableFloatMap = Field(default_factory=dict)
    sigma_gap: NullableFloatMap = Field(default_factory=dict)
    sector_relative_strength_percentile: float | None = None
    ticker_return_4w: float | None = None
    sector_return_4w: float | None = None
    short_history_flag: bool = False
    split_adjustment_flag: bool = False
    price_history_sessions_750d: int | None = None
    price_history_coverage_750d: float | None = None

    @field_validator(
        "price_change_1d",
        "price_change_5d",
        "price_change_20d",
        "price_change_60d",
        "gap_from_52w_low",
        "turnover_spike_5d",
        "sector_relative_strength_4w",
        "sector_relative_strength_percentile",
        "ticker_return_4w",
        "sector_return_4w",
        "price_history_coverage_750d",
    )
    @classmethod
    def _finite_optional_float(cls, value: float | None) -> float | None:
        return _validate_finite(value)


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class EvidenceHit:
    name: NonEmptyString
    playbook_id: NonEmptyString
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
    evidence_hits: tuple[EvidenceHit, ...] = ()
    failure_reasons: tuple[str, ...] = ()
    null_reasons: tuple[str, ...] = ()

    @field_validator("evidence_hits", "failure_reasons", "null_reasons", mode="before")
    @classmethod
    def _tuple_sequence(cls, value: Sequence[Any]) -> tuple[Any, ...]:
        return tuple(value)

    @model_validator(mode="after")
    def _consistent_result(self) -> ScreeningResult:
        if self.pass_fail and not self.evidence_hits:
            raise ValueError("pass_fail=True requires at least one evidence_hit")
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
    evidence_hits: tuple[EvidenceHit, ...]
    ttm_quality: Mapping[str, TTMQuality]
    market_cap_oku: int | None = None
    avg_turnover_oku: float | None = None
    listing_span_days: int | None = None
    jpx_flags: tuple[str, ...] = ()
    price_change_1d: float | None = None
    price_change_5d: float | None = None
    price_change_20d: float | None = None
    price_change_60d: float | None = None
    gap_from_52w_low: float | None = None
    turnover_spike_5d: float | None = None
    sector_relative_strength_percentile: float | None = None
    price_history_sessions_750d: int | None = None
    price_history_coverage_750d: float | None = None
    metrics: MetricValueMap = Field(default_factory=dict)
    next_earnings_date: date | None = None
    split_adjustment_flag: bool = False
    freshness_warnings: tuple[FreshnessWarning, ...] = ()

    @field_validator("evidence_hits", "freshness_warnings", "jpx_flags", mode="before")
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
        "price_change_1d",
        "price_change_5d",
        "price_change_20d",
        "price_change_60d",
        "gap_from_52w_low",
        "turnover_spike_5d",
        "sector_relative_strength_percentile",
        "price_history_coverage_750d",
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
    generated_by: str = "screening-cli-v1"
    data_sources: tuple[str, ...] = (
        "j-quants-light",
        "jpx-public-regulation",
    )
    provider_status_lines: tuple[str, ...] = ()
    universe_exclusion_lines: tuple[str, ...] = ()
    ttm_quality_counts: Mapping[str, int] = Field(default_factory=dict)
    evidence_hits_summary: Mapping[str, int] = Field(default_factory=dict)
    fallback_lines: tuple[str, ...] = ()

    @field_validator(
        "candidates",
        "data_sources",
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
