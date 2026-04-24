from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Mapping, Sequence


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


@dataclass(frozen=True)
class SecurityMaster:
    code: str
    name: str
    market_segment: str
    sector_33: str
    is_common_stock: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", normalize_ticker(self.code))


@dataclass(frozen=True)
class UniverseSnapshot:
    market_cap_oku: int | None
    avg_turnover_oku: float | None
    exclusion_flags: Sequence[str] = field(default_factory=tuple)


@dataclass(frozen=True)
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
    debt: float | None
    cash: float | None
    ebitda_ttm: float | None
    consolidation_basis: str | None
    operating_profit: float | None = None
    operating_profit_source: OperatingProfitSource = OperatingProfitSource.NULL
    eps_yoy: float | None = None
    sales_yoy: float | None = None
    operating_profit_yoy: float | None = None
    ttm_quality_ev_ebitda: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_p_s: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_pcfr: TTMQuality = TTMQuality.UNAVAILABLE


@dataclass(frozen=True)
class DerivedMetrics:
    price_change_60d: float | None
    sector_relative_strength_4w: float | None
    sector_median_gap: Mapping[str, float | None] = field(default_factory=dict)
    self_range_percentile: Mapping[str, float | None] = field(default_factory=dict)
    sigma_gap: Mapping[str, float | None] = field(default_factory=dict)
    sector_relative_strength_percentile: float | None = None
    ticker_return_4w: float | None = None
    sector_return_4w: float | None = None
    short_history_flag: bool = False


@dataclass(frozen=True)
class ScreeningResult:
    pass_fail: bool
    threshold_hit: Sequence[str] = field(default_factory=tuple)
    failure_reasons: Sequence[str] = field(default_factory=tuple)
    null_reasons: Sequence[str] = field(default_factory=tuple)


@dataclass(frozen=True)
class ScreenedTicker:
    ticker: str
    name: str
    per_forward: float | None
    per_trailing: float | None
    pbr: float | None
    ev_ebitda: float | None
    p_s: float | None
    pcfr: float | None
    sector_33: str
    threshold_hit: Sequence[str]
    ttm_quality: Mapping[str, TTMQuality]

    def __post_init__(self) -> None:
        object.__setattr__(self, "ticker", normalize_ticker(self.ticker))


@dataclass(frozen=True)
class ScreenedRunDocument:
    run_date: date
    asof_date: date
    universe_size: int
    filters: Mapping[str, object]
    tickers: Sequence[ScreenedTicker]
    run_at: datetime
    generated_by: str = "screening-cli-v1"
    data_sources: Sequence[str] = (
        "j-quants-light",
        "edinet-api-v2@2026-01-29",
        "jpx-public-csv",
    )
    fact_memo_lines: Sequence[str] = field(default_factory=tuple)
    provider_status_lines: Sequence[str] = field(default_factory=tuple)
    universe_exclusion_lines: Sequence[str] = field(default_factory=tuple)
    ttm_quality_counts: Mapping[str, int] = field(default_factory=dict)
    fallback_lines: Sequence[str] = field(default_factory=tuple)
