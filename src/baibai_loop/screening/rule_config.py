from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from baibai_loop.foundation.yaml_io import safe_load

DEFAULT_RULES_PATH = Path("records/_config/screening-rules/2026-06-19T000000+0900.yaml")

BUILTIN_SELECTION_PROFILES = frozenset({"balanced"})


class UniverseRules(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

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


class ValuationReversionPlaybook(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    playbook_id: str
    excluded_sectors: tuple[str, ...] = ()
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

    @field_validator("excluded_sectors", mode="before")
    @classmethod
    def _tuple_excluded_sectors(cls, value: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
        return tuple(value or ())


class CashRichPlaybook(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    playbook_id: str
    excluded_sectors: tuple[str, ...] = ()
    cash_to_market_cap_min: float = Field(ge=0)
    edinet_net_cash_to_market_cap_min_if_available: float | None = None
    price_to_equity_max: float = Field(ge=0)
    equity_ratio_min: float = Field(ge=0, le=1)
    operating_profit_positive_required: bool
    operating_profit_yoy_deterioration_threshold: float | None = None

    @field_validator("excluded_sectors", mode="before")
    @classmethod
    def _tuple_excluded_sectors(cls, value: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
        return tuple(value or ())


class CashflowYieldPlaybook(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    playbook_id: str
    excluded_sectors: tuple[str, ...] = ()
    ocf_yield_min: float = Field(ge=0)
    ttm_cfo_required: bool
    cfo_yoy_min: float
    cfo_yoy_required: bool
    operating_profit_yoy_deterioration_threshold: float | None = None
    fcf_yield_required_positive: bool = False

    @field_validator("excluded_sectors", mode="before")
    @classmethod
    def _tuple_excluded_sectors(cls, value: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
        return tuple(value or ())


class SalesDiscountGrowthPlaybook(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    playbook_id: str
    excluded_sectors: tuple[str, ...] = ()
    ps_sector_gap_max: float
    sales_yoy_min: float
    allow_operating_loss_if_cfo_positive_or_loss_narrowing: bool
    operating_margin_min: float | None = None

    @field_validator("excluded_sectors", mode="before")
    @classmethod
    def _tuple_excluded_sectors(cls, value: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
        return tuple(value or ())


class OutputRules(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    research_selection_target_max: int = Field(ge=0)
    research_selection_playbook_order: tuple[str, ...]

    @field_validator("research_selection_playbook_order", mode="before")
    @classmethod
    def _tuple_research_selection_playbook_order(
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

    @model_validator(mode="after")
    def _requires_price_dislocation_trigger(self) -> FastDislocationRules:
        if not self.enabled:
            return self
        if all(
            threshold is None
            for threshold in (
                self.price_change_1d_max,
                self.price_change_5d_max,
                self.price_change_20d_max,
                self.price_change_60d_max,
            )
        ):
            raise ValueError(
                "fast_dislocation enabled profiles must configure at least one price_change_* "
                "threshold; gap_from_52w_low and turnover_spike_5d are auxiliary only"
            )
        return self


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
    max_recommended_per_playbook: int = Field(default=2, ge=1)
    max_previous_candidates_in_recommended: int | None = Field(default=2, ge=0)
    previous_overlap_warning_ratio: float = Field(default=0.6, ge=0, le=1)


class SelectionLiquidityRules(BaseModel):
    """Analysis-layer scope parameters applied when ranking research candidates.

    The screen itself covers every common stock; size, liquidity, seasoning,
    and trading-restriction exclusions are applied here so they stay visible,
    configurable facts instead of silently narrowing the data.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    min_market_cap_oku: float = Field(default=100, ge=0)
    min_avg_turnover_oku: float = Field(default=1.0, ge=0)
    min_listing_span_days: int = Field(default=182, ge=0)
    exclude_jpx_flagged: bool = True

    def matches(
        self,
        *,
        market_cap_oku: float | None,
        avg_turnover_oku: float | None,
        listing_span_days: float | None,
        jpx_flags: Sequence[str] | None,
        required_jpx_flags: frozenset[str],
        require_facts: bool,
    ) -> bool:
        """Single predicate for the investable set, shared by the median
        population (``require_facts=True``: a missing fact disqualifies) and
        the selection filter (``require_facts=False``: a missing fact passes
        and is surfaced separately in diagnostics)."""
        facts = (market_cap_oku, avg_turnover_oku, listing_span_days, jpx_flags)
        if require_facts and any(value is None for value in facts):
            return False
        if market_cap_oku is not None and market_cap_oku < self.min_market_cap_oku:
            return False
        if avg_turnover_oku is not None and avg_turnover_oku < self.min_avg_turnover_oku:
            return False
        if listing_span_days is not None and listing_span_days < self.min_listing_span_days:
            return False
        return not (
            self.exclude_jpx_flagged and jpx_flags and required_jpx_flags.intersection(jpx_flags)
        )


class SelectionRules(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    default_profile: str = "balanced"
    liquidity: SelectionLiquidityRules = Field(default_factory=SelectionLiquidityRules)
    fast_dislocation: FastDislocationRules = Field(default_factory=FastDislocationRules)
    long_hold_survivability: LongHoldSurvivabilityRules = Field(
        default_factory=LongHoldSurvivabilityRules
    )
    diversity: SelectionDiversityRules = Field(default_factory=SelectionDiversityRules)

    @field_validator("default_profile")
    @classmethod
    def _known_default_profile(cls, value: str) -> str:
        if value not in BUILTIN_SELECTION_PROFILES:
            raise ValueError(
                "unknown default selection profile: "
                f"{value}; expected one of {', '.join(sorted(BUILTIN_SELECTION_PROFILES))}"
            )
        return value


class ScreeningRules(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    universe: UniverseRules
    ttm: TTMRules
    quality: QualityRules
    screening_playbooks: Mapping[
        str,
        ValuationReversionPlaybook
        | CashRichPlaybook
        | CashflowYieldPlaybook
        | SalesDiscountGrowthPlaybook,
    ]
    output: OutputRules
    selection: SelectionRules = Field(default_factory=SelectionRules)

    @field_validator("screening_playbooks", mode="before")
    @classmethod
    def _coerce_screening_playbooks(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError("screening_playbooks must be a mapping")
        playbooks: dict[str, Any] = {}
        for name, raw in value.items():
            if not isinstance(raw, Mapping):
                raise ValueError(f"evidence_hit playbook {name!r} must be a mapping")
            data = dict(raw)
            match name:
                case "valuation-reversion":
                    playbooks[name] = ValuationReversionPlaybook.model_validate(data)
                case "cash-rich-asset-discount":
                    playbooks[name] = CashRichPlaybook.model_validate(data)
                case "cashflow-yield-discount":
                    playbooks[name] = CashflowYieldPlaybook.model_validate(data)
                case "sales-discount-growth":
                    playbooks[name] = SalesDiscountGrowthPlaybook.model_validate(data)
                case _:
                    raise ValueError(f"unknown evidence_hit playbook: {name}")
        return playbooks

    @property
    def playbook_order(self) -> tuple[str, ...]:
        return tuple(self.screening_playbooks.keys())

    @model_validator(mode="after")
    def _validate_output_playbook_order(self) -> ScreeningRules:
        unknown = set(self.output.research_selection_playbook_order) - set(self.screening_playbooks)
        if unknown:
            joined = ", ".join(sorted(unknown))
            raise ValueError(f"unknown research selection playbook(s): {joined}")
        return self


def load_screening_rules(path: Path = DEFAULT_RULES_PATH) -> ScreeningRules:
    if not path.exists() and not path.is_absolute():
        path = Path(__file__).resolve().parents[3] / path
    payload = safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"screening rules root must be a mapping: {path}")
    return ScreeningRules.model_validate(payload)
