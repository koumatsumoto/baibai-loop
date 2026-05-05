from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_RULES_PATH = Path("records/_config/screening-rules.yaml")


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

    playbook: str
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

    playbook: str
    cash_to_market_cap_min: float = Field(ge=0)
    price_to_equity_max: float = Field(ge=0)
    operating_profit_positive_required: bool


class CashflowYieldLane(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    playbook: str
    ocf_yield_min: float = Field(ge=0)
    ttm_cfo_required: bool


class SalesDiscountGrowthLane(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    playbook: str
    ps_sector_gap_max: float
    sales_yoy_min: float
    allow_operating_loss_if_cfo_positive_or_loss_narrowing: bool


class OutputRules(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    research_selection_target_min: int = Field(ge=0)
    research_selection_target_max: int = Field(ge=0)


class ScreeningRules(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    universe: UniverseRules
    ttm: TTMRules
    quality: QualityRules
    signal_lanes: Mapping[
        str, ValuationReversionLane | CashRichLane | CashflowYieldLane | SalesDiscountGrowthLane
    ]
    output: OutputRules

    @field_validator("signal_lanes", mode="before")
    @classmethod
    def _coerce_signal_lanes(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError("signal_lanes must be a mapping")
        lanes: dict[str, Any] = {}
        for name, raw in value.items():
            if not isinstance(raw, Mapping):
                raise ValueError(f"signal lane {name!r} must be a mapping")
            data = dict(raw)
            match name:
                case "valuation-reversion":
                    lanes[name] = ValuationReversionLane.model_validate(data)
                case "cash-rich-asset-discount":
                    lanes[name] = CashRichLane.model_validate(data)
                case "cashflow-yield-discount":
                    lanes[name] = CashflowYieldLane.model_validate(data)
                case "sales-discount-growth":
                    lanes[name] = SalesDiscountGrowthLane.model_validate(data)
                case _:
                    raise ValueError(f"unknown signal lane: {name}")
        return lanes

    @property
    def lane_order(self) -> tuple[str, ...]:
        return tuple(self.signal_lanes.keys())


def load_screening_rules(path: Path = DEFAULT_RULES_PATH) -> ScreeningRules:
    if not path.exists() and not path.is_absolute():
        path = Path(__file__).resolve().parents[3] / path
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"screening rules root must be a mapping: {path}")
    return ScreeningRules.model_validate(payload)
