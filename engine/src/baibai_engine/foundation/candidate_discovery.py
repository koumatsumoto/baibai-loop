"""Stop T1 candidate drift by typing the coordinates shared across L2/L3 boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Nomination(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    valuation_approach_id: str = Field(min_length=1)
    valuation_method_id: str = Field(min_length=1)
    rank: int = Field(ge=1)


class ReviewSetMethod(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    method_id: str = Field(min_length=1)
    method_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_capacity: int = Field(gt=0)
    nomination_depth: int = Field(gt=0)
    representation_targets: Mapping[str, int]


class _StrictAnalysisGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class IdentityLiquidityAnalysis(_StrictAnalysisGroup):
    market_cap_oku: float | int | None
    avg_turnover_oku: float | int | None
    listing_span_days: float | int | None
    jpx_flags: tuple[str, ...] | None

    @field_validator("jpx_flags", mode="before")
    @classmethod
    def _tuple_jpx_flags(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class ValuationAnalysis(_StrictAnalysisGroup):
    per_forward: float | int | None
    per_trailing: float | int | None
    pbr: float | int | None
    ev_ebitda: float | int | None
    p_s: float | int | None
    pcfr: float | int | None


class CurrentEarningsAnalysis(_StrictAnalysisGroup):
    fcf_yield: float | int | None
    ocf_yield: float | int | None
    forecast_special_gain_flag: bool | None
    forecast_full_year_loss_flag: bool | None


class NormalizedEarningsAnalysis(_StrictAnalysisGroup):
    normalized_per_3fy: float | int | None
    normalized_per_3fy_sector_gap: float | int | None


class AssetValueAnalysis(_StrictAnalysisGroup):
    asset_backed_ratio: float | int | None
    net_cash_to_market_cap: float | int | None
    investment_securities: float | int | None
    equity_ratio: float | int | None


class ReinvestmentAnalysis(_StrictAnalysisGroup):
    p_s_sector_gap: float | int
    operating_return_on_capital_proxy: float | int
    sales_yoy: float | int
    operating_margin: float | int
    fcf_yield: float | int


class ExpectedReturnAnalysis(_StrictAnalysisGroup):
    er_annual: float | int | None
    er_reversion_annual: float | int | None
    er_carry_annual: float | int | None
    fv_sector_median_yen: float | int | None
    fv_self_range_yen: float | int | None
    er_origin: str | None
    er_model_version: str | None
    er_unit: str | None
    er_assumptions: str | None


class DataQualityAnalysis(_StrictAnalysisGroup):
    bs_carry_forward_fields: str | None
    bs_carry_forward_lag_days: float | int | None
    edinet_failure_reasons: str | None
    stale_fin_flag: bool | None


class ContextAnalysis(_StrictAnalysisGroup):
    next_earnings_status: str | None
    next_earnings_estimated_date: str | None
    margin_short_to_adv: float | int | None
    tse_capital_policy_status: str | None
    large_holding_event_recent: bool | None
    tender_offer_event_recent: bool | None


class ReviewSetAnalysis(_StrictAnalysisGroup):
    identity_liquidity: IdentityLiquidityAnalysis
    valuation: ValuationAnalysis
    current_earnings: CurrentEarningsAnalysis
    normalized_earnings: NormalizedEarningsAnalysis
    asset_value: AssetValueAnalysis
    reinvestment: ReinvestmentAnalysis | None
    expected_return: ExpectedReturnAnalysis
    data_quality: DataQualityAnalysis
    context: ContextAnalysis


class ReviewSetEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    review_position: int = Field(ge=1)
    ticker: str = Field(pattern=r"^[0-9A-Z]{4}$")
    name: str
    sector_33: str
    nominations: tuple[Nomination, ...]
    support_count: int = Field(ge=1, le=4)
    rank_vector: tuple[int, int, int, int]
    analysis: ReviewSetAnalysis

    @field_validator("nominations", mode="before")
    @classmethod
    def _tuple_nominations(cls, value: object) -> tuple[object, ...]:
        if not isinstance(value, list | tuple):
            raise ValueError("nominations must be an array")
        return tuple(value)

    @field_validator("rank_vector", mode="before")
    @classmethod
    def _tuple_rank_vector(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _validate_support(self) -> Self:
        if not self.nominations or len(self.nominations) != self.support_count:
            raise ValueError("support count must equal the non-empty nominations")
        approaches = [item.valuation_approach_id for item in self.nominations]
        if len(approaches) != len(set(approaches)):
            raise ValueError("a ticker cannot have duplicate approach nominations")
        return self


__all__ = [
    "Nomination",
    "ReviewSetAnalysis",
    "ReviewSetEntry",
    "ReviewSetMethod",
]
