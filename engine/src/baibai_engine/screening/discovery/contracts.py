"""Candidate Discoveryの純粋な型・4 Approach identity・Review Set保存契約。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

APPROACH_IDS = (
    "current-earnings-power",
    "normalized-earnings-power",
    "asset-value",
    "reinvestment-value",
)


class Nomination(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    valuation_approach_id: str = Field(min_length=1)
    valuation_method_id: str = Field(min_length=1)
    rank: int = Field(ge=1)


class CandidateDiscoveryMethodIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    method_id: str = Field(min_length=1)
    method_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    nomination_depth: int = Field(gt=0)


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
    dividend_yield: float | int | None = None
    dividend_basis: str | None = None
    net_share_change_yoy: float | int | None = None
    carry_missing_assumption: str | None = None

    @model_serializer(mode="wrap")
    def _preserve_saved_carry_inputs(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        # A frozen publication's absent fields must not become explicit nulls:
        # its original candidate/input hash and judgment meaning stay unchanged.
        result: dict[str, Any] = handler(self)
        for key in (
            "dividend_yield",
            "dividend_basis",
            "net_share_change_yoy",
            "carry_missing_assumption",
        ):
            if key not in self.model_fields_set:
                result.pop(key, None)
        return result


class DataQualityAnalysis(_StrictAnalysisGroup):
    bs_carry_forward_fields: str | None
    bs_carry_forward_lag_days: float | int | None
    edinet_failure_reasons: str | None
    stale_fin_flag: bool | None
    ttm_quality_ev_ebitda: Literal["exact", "approximated", "unavailable"] | None = None
    ttm_quality_fcf: Literal["exact", "approximated", "unavailable"] | None = None

    @model_serializer(mode="wrap")
    def _preserve_absent_quality(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # 保存済みsnapshotの欠如をnullへ変えるとcanonical/input hashが変わる。
        # 新しいsnapshotが明示するnullは品質未取得としてそのまま出す。
        result: dict[str, Any] = handler(self)
        for key in ("ttm_quality_ev_ebitda", "ttm_quality_fcf"):
            if key not in self.model_fields_set:
                result.pop(key, None)
        return result


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

    ticker: str = Field(pattern=r"^[0-9A-Z]{4}$")
    name: str
    sector_33: str
    nominations: tuple[Nomination, ...]
    analysis: ReviewSetAnalysis

    @field_validator("nominations", mode="before")
    @classmethod
    def _tuple_nominations(cls, value: object) -> tuple[object, ...]:
        if not isinstance(value, list | tuple):
            raise ValueError("nominations must be an array")
        return tuple(value)

    @model_validator(mode="after")
    def _validate_support(self) -> Self:
        if not self.nominations:
            raise ValueError("review set entry must contain a nomination")
        approaches = [item.valuation_approach_id for item in self.nominations]
        if len(approaches) != len(set(approaches)):
            raise ValueError("a ticker cannot have duplicate approach nominations")
        return self


class PublishedReviewSet(BaseModel):
    """Typed L2 publication rebuilt only from a screening run and its rules."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[2]
    kind: Literal["review_set"]
    review_set_id: str = Field(min_length=1)
    run_revision_id: str = Field(min_length=1)
    as_of: date
    created_at: datetime
    screening_rules_hash: str = Field(min_length=1)
    method: CandidateDiscoveryMethodIdentity
    entries: tuple[ReviewSetEntry, ...]
    diagnostics: Mapping[str, object]

    @field_validator("as_of", mode="before")
    @classmethod
    def _parse_as_of(cls, value: object) -> object:
        return date.fromisoformat(value) if isinstance(value, str) else value

    @field_validator("created_at", mode="before")
    @classmethod
    def _parse_created_at(cls, value: object) -> object:
        return datetime.fromisoformat(value) if isinstance(value, str) else value

    @field_validator("entries", mode="before")
    @classmethod
    def _tuple_entries(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _validate_publication(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        tickers = [entry.ticker for entry in self.entries]
        if tickers != sorted(tickers) or len(tickers) != len(set(tickers)):
            raise ValueError("review set entries must be unique and ordered by ticker")
        if len(self.entries) > len(APPROACH_IDS) * self.method.nomination_depth:
            raise ValueError("review set exceeds the configured Valuation Approach union bound")
        ranks_by_approach: dict[str, list[int]] = {approach: [] for approach in APPROACH_IDS}
        for entry in self.entries:
            for nomination in entry.nominations:
                if nomination.valuation_approach_id not in ranks_by_approach:
                    raise ValueError("review set contains an unknown valuation Approach")
                ranks_by_approach[nomination.valuation_approach_id].append(nomination.rank)
        for approach, ranks in ranks_by_approach.items():
            if sorted(ranks) != list(range(1, len(ranks) + 1)):
                raise ValueError(f"{approach} nomination ranks must be contiguous from 1")
            if len(ranks) > self.method.nomination_depth:
                raise ValueError(f"{approach} exceeds nomination_depth")
        return self


__all__ = [
    "APPROACH_IDS",
    "CandidateDiscoveryMethodIdentity",
    "Nomination",
    "PublishedReviewSet",
    "ReviewSetAnalysis",
    "ReviewSetEntry",
]
