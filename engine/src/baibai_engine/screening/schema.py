from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from math import isfinite
from typing import Annotated, Any

from pydantic import ConfigDict, Field, field_validator, model_validator
from pydantic.dataclasses import dataclass

from baibai_engine.market.metric_quality import TTMQuality
from baibai_engine.market.ticker import normalize_ticker as normalize_ticker

from .discovery.contracts import StockCandidateContext

_MODEL_CONFIG = ConfigDict(
    strict=True,
    arbitrary_types_allowed=False,
    validate_assignment=False,
)
_TICKER_PATTERN = r"^[0-9A-Z]{4}$"

type NullableFloatMap = Mapping[str, float | None]
type StringMap = Mapping[str, str]
type MetricValueMap = Mapping[str, float | int | bool | str | None]
type Ticker = Annotated[str, Field(pattern=_TICKER_PATTERN)]
type NonEmptyString = Annotated[str, Field(min_length=1)]
type NonNegativeInt = Annotated[int, Field(ge=0)]


def _validate_finite(value: float | None) -> float | None:
    if value is not None and not isfinite(value):
        raise ValueError("numeric values must be finite")
    return value


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
    # 上の 2 つがどの母集団から作られたかを軸ごとに記録する。母数が薄い業種では
    # 市場全体へ落ちるので、同じ field が「業種との差」と「市場との差」の 2 つの量を
    # 指す。落ちた業種は市場より低倍率に寄るため、素性が無いと gate を越えた根拠が
    # 業種比較なのか市場比較なのか読めない。値は `SECTOR_MEDIAN_BASIS_SECTOR` /
    # `SECTOR_MEDIAN_BASIS_MARKET` のいずれか。
    sector_median_basis: StringMap = Field(default_factory=dict)
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
    # Supply/demand read from the exchange's published margin balances. Definitions
    # and the reason each one is shaped this way live in `margin_metrics`. The field
    # names the balance date behind the numbers, which is what makes their age
    # readable: through 2026-09-18 the balances are weekly and published days later,
    # so an asof in the middle of a week is looking at data up to nine days old by
    # construction; the daily series that replaces them narrows that to a day.
    margin_week_end: date | None = None
    margin_issue_type: str | None = None
    margin_long_to_adv: float | None = None
    margin_short_to_adv: float | None = None
    margin_long_share: float | None = None
    margin_long_delta_26w: float | None = None
    margin_std_long_share: float | None = None
    # Annualized 60-session realized volatility is a calibration control. It is
    # kept out of Security Analysis output until a separately tested decision use exists.
    realized_volatility_60d: float | None = None

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
        "margin_long_to_adv",
        "margin_short_to_adv",
        "margin_long_share",
        "margin_long_delta_26w",
        "margin_std_long_share",
        "realized_volatility_60d",
    )
    @classmethod
    def _finite_optional_float(cls, value: float | None) -> float | None:
        return _validate_finite(value)


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
class StockAnalysis:
    ticker: Ticker
    name: NonEmptyString
    per_forward: float | None
    per_trailing: float | None
    pbr: float | None
    ev_ebitda: float | None
    p_s: float | None
    pcfr: float | None
    sector_33: NonEmptyString
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
    candidate: StockCandidateContext | None = None

    @field_validator("freshness_warnings", "jpx_flags", mode="before")
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
class ScreeningRunDocument:
    run_date: date
    asof_date: date
    universe_size: NonNegativeInt
    filters: Mapping[str, Any]
    stock_analyses: tuple[StockAnalysis, ...]
    run_at: datetime
    run_id: NonEmptyString
    screening_rules_hash: NonEmptyString
    er_model_version: NonEmptyString
    generated_by: str = "screening-cli-v1"
    data_sources: tuple[str, ...] = (
        "j-quants-light",
        "jpx-public-earnings-calendar",
        "jpx-public-regulation",
    )
    provider_status_lines: tuple[str, ...] = ()
    universe_exclusion_lines: tuple[str, ...] = ()
    ttm_quality_counts: Mapping[str, int] = Field(default_factory=dict)
    fallback_lines: tuple[str, ...] = ()

    @field_validator(
        "stock_analyses",
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
    def _consistent_dates(self) -> ScreeningRunDocument:
        if self.run_date != self.asof_date:
            raise ValueError("run_date must equal asof_date")
        if self.run_at.tzinfo is None:
            raise ValueError("run_at must be timezone-aware")
        return self
