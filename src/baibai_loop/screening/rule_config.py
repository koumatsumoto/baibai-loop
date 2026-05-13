from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_RULES_PATH = Path("records/_config/screening-rules/2026-05-01T000000+0900.yaml")


class UniverseRules(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    min_market_cap_oku: int = Field(ge=0)
    min_avg_turnover_oku: float = Field(ge=0)
    listed_under_days: int = Field(ge=0)
    required_jpx_flags: tuple[str, ...]

    @field_validator("required_jpx_flags", mode="before")
    @classmethod
    def _tuple_flags(cls, value: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        return tuple(value)


class TTMRules(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    full_year_min_days: int = Field(ge=1)
    full_year_max_days: int = Field(ge=1)
    period_length_tolerance_ratio: float = Field(ge=0)
    period_end_tolerance_days: int = Field(ge=0)


class QualityRules(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    partial_warning_ttm_ratio: float = Field(ge=0)
    partial_warning_ttm_count: int = Field(ge=0)
    partial_warning_yoy_missing_ratio: float = Field(ge=0)
    yoy_deterioration_threshold: float


class ValuationReversionLane(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    playbook_id: str
    sector_median_gap_max: float
    self_range_percentile_max: float = Field(ge=0, le=1)
    price_change_60d_max: float
    sigma_gap_max: float
    sector_relative_strength_percentile_max: float = Field(ge=0, le=1)
    metrics: tuple[str, ...]

    @field_validator("metrics", mode="before")
    @classmethod
    def _tuple_metrics(cls, value: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        return tuple(value)


class CashRichLane(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    playbook_id: str
    excluded_sectors: tuple[str, ...] = ()
    cash_to_market_cap_min: float = Field(ge=0)
    edinet_net_cash_to_market_cap_min_if_available: float | None = None
    price_to_equity_max: float = Field(ge=0)
    equity_ratio_min: float = Field(ge=0, le=1)
    operating_profit_positive_required: bool

    @field_validator("excluded_sectors", mode="before")
    @classmethod
    def _tuple_excluded_sectors(cls, value: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
        return tuple(value or ())


class CashflowYieldLane(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    playbook_id: str
    excluded_sectors: tuple[str, ...] = ()
    ocf_yield_min: float = Field(ge=0)
    ttm_cfo_required: bool
    cfo_yoy_min: float
    cfo_yoy_required: bool

    @field_validator("excluded_sectors", mode="before")
    @classmethod
    def _tuple_excluded_sectors(cls, value: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
        return tuple(value or ())


class StrictNetCashLane(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    playbook_id: str
    excluded_sectors: tuple[str, ...] = ()
    net_cash_to_market_cap_min: float
    price_to_equity_max: float = Field(ge=0)
    equity_ratio_min: float = Field(ge=0, le=1)
    operating_profit_positive_required: bool

    @field_validator("excluded_sectors", mode="before")
    @classmethod
    def _tuple_excluded_sectors(cls, value: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
        return tuple(value or ())


class FcfYieldLane(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    playbook_id: str
    excluded_sectors: tuple[str, ...] = ()
    fcf_yield_min: float = Field(ge=0)
    fcf_required: bool
    cfo_yoy_min: float
    cfo_yoy_required: bool

    @field_validator("excluded_sectors", mode="before")
    @classmethod
    def _tuple_excluded_sectors(cls, value: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
        return tuple(value or ())


class SalesDiscountGrowthLane(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    playbook_id: str
    excluded_sectors: tuple[str, ...] = ()
    ps_sector_gap_max: float
    sales_yoy_min: float
    allow_operating_loss_if_cfo_positive_or_loss_narrowing: bool

    @field_validator("excluded_sectors", mode="before")
    @classmethod
    def _tuple_excluded_sectors(cls, value: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
        return tuple(value or ())


class OutputRules(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    research_selection_target_min: int = Field(ge=0)
    research_selection_target_max: int = Field(ge=0)
    lane_toplist_limit: int = Field(default=5, ge=1)
    research_selection_lane_order: tuple[str, ...]

    @field_validator("research_selection_lane_order", mode="before")
    @classmethod
    def _tuple_research_selection_lane_order(
        cls, value: list[str] | tuple[str, ...]
    ) -> tuple[str, ...]:
        return tuple(value)


class FastDislocationRules(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    enabled: bool = True
    price_change_1d_max: float | None = None
    price_change_5d_max: float | None = None
    price_change_20d_max: float | None = None
    price_change_60d_max: float | None = None
    gap_from_52w_low_max: float | None = None
    turnover_spike_5d_min: float | None = None
    min_fundamental_guard_count: int = Field(default=2, ge=0)
    min_fundamental_guard_family_count: int = Field(default=2, ge=0)
    high_confidence_guard_count: int = Field(default=3, ge=0)
    high_confidence_guard_family_count: int = Field(default=2, ge=0)
    ocf_yield_min: float = Field(default=0.08, ge=0)
    fcf_yield_min: float = Field(default=0.05, ge=0)
    price_to_equity_max: float = Field(default=1.0, ge=0)
    equity_ratio_min: float = Field(default=0.4, ge=0, le=1)
    net_cash_to_market_cap_min: float = 0.2
    sales_yoy_min: float = 0.05
    operating_profit_positive_required: bool = True


class LongHoldSurvivabilityRules(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    high_min_support_count: int = Field(default=4, ge=1)
    medium_min_support_count: int = Field(default=2, ge=1)
    equity_ratio_high_min: float = Field(default=0.5, ge=0, le=1)
    equity_ratio_medium_min: float = Field(default=0.35, ge=0, le=1)
    net_cash_to_market_cap_high_min: float = 0.2
    net_cash_to_market_cap_medium_min: float = 0.0
    cash_to_market_cap_high_min: float = Field(default=0.3, ge=0)
    ocf_yield_positive_min: float = 0.0
    fcf_yield_positive_min: float = 0.0
    min_avg_turnover_oku: float = Field(default=1.0, ge=0)


class SelectionDiversityRules(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    max_recommended_per_sector: int = Field(default=1, ge=1)
    max_recommended_per_lane: int = Field(default=2, ge=1)
    max_recommended_per_queue: int | None = Field(default=3, ge=1)
    max_previous_candidates_in_recommended: int | None = Field(default=2, ge=0)
    previous_overlap_warning_ratio: float = Field(default=0.6, ge=0, le=1)


class SelectionRules(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    default_profile: str = "balanced"
    queue_order: tuple[str, ...] = (
        "fast_dislocation_queue",
        "core_value_queue",
        "long_hold_survivability_queue",
    )
    fast_dislocation: FastDislocationRules = Field(default_factory=FastDislocationRules)
    long_hold_survivability: LongHoldSurvivabilityRules = Field(
        default_factory=LongHoldSurvivabilityRules
    )
    diversity: SelectionDiversityRules = Field(default_factory=SelectionDiversityRules)
    ai_exposure_sector_tags: Mapping[str, tuple[str, ...]] = Field(default_factory=dict)

    @field_validator("queue_order", mode="before")
    @classmethod
    def _tuple_queue_order(cls, value: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        return tuple(value)

    @field_validator("queue_order")
    @classmethod
    def _known_queue_order(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        known = {
            "core_value_queue",
            "fast_dislocation_queue",
            "long_hold_survivability_queue",
        }
        unknown = sorted(set(value) - known)
        if unknown:
            raise ValueError("unknown selection queue(s): " + ", ".join(unknown))
        return value

    @field_validator("ai_exposure_sector_tags", mode="before")
    @classmethod
    def _tuple_ai_exposure_tags(cls, value: Mapping[str, Any] | None) -> dict[str, tuple[str, ...]]:
        if not value:
            return {}
        return {str(sector): tuple(tags) for sector, tags in value.items()}


class ScreeningRules(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    universe: UniverseRules
    ttm: TTMRules
    quality: QualityRules
    screening_playbooks: Mapping[
        str,
        ValuationReversionLane
        | CashRichLane
        | CashflowYieldLane
        | StrictNetCashLane
        | FcfYieldLane
        | SalesDiscountGrowthLane,
    ]
    output: OutputRules
    selection: SelectionRules = Field(default_factory=SelectionRules)

    @field_validator("screening_playbooks", mode="before")
    @classmethod
    def _coerce_screening_playbooks(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError("screening_playbooks must be a mapping")
        lanes: dict[str, Any] = {}
        for name, raw in value.items():
            if not isinstance(raw, Mapping):
                raise ValueError(f"evidence_hit lane {name!r} must be a mapping")
            data = dict(raw)
            match name:
                case "valuation-reversion":
                    lanes[name] = ValuationReversionLane.model_validate(data)
                case "cash-rich-asset-discount":
                    lanes[name] = CashRichLane.model_validate(data)
                case "cashflow-yield-discount":
                    lanes[name] = CashflowYieldLane.model_validate(data)
                case "strict-net-cash-discount":
                    lanes[name] = StrictNetCashLane.model_validate(data)
                case "fcf-yield-discount":
                    lanes[name] = FcfYieldLane.model_validate(data)
                case "sales-discount-growth":
                    lanes[name] = SalesDiscountGrowthLane.model_validate(data)
                case _:
                    raise ValueError(f"unknown evidence_hit lane: {name}")
        return lanes

    @property
    def lane_order(self) -> tuple[str, ...]:
        return tuple(self.screening_playbooks.keys())

    @model_validator(mode="after")
    def _validate_output_lane_order(self) -> ScreeningRules:
        unknown = set(self.output.research_selection_lane_order) - set(self.screening_playbooks)
        if unknown:
            joined = ", ".join(sorted(unknown))
            raise ValueError(f"unknown research selection lane(s): {joined}")
        return self


def load_screening_rules(path: Path = DEFAULT_RULES_PATH) -> ScreeningRules:
    if not path.exists() and not path.is_absolute():
        path = Path(__file__).resolve().parents[3] / path
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"screening rules root must be a mapping: {path}")
    return ScreeningRules.model_validate(payload)
