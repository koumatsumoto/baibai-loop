from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from baibai_engine.foundation.repository_layout import SCREENING_RULES_PATH
from baibai_engine.foundation.yaml_io import safe_load

DEFAULT_RULES_PATH = SCREENING_RULES_PATH


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


class ValuationReversionEvidencePattern(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    evidence_pattern_id: str
    excluded_sectors: tuple[str, ...] = ()
    sector_median_gap_max: float
    self_range_percentile_max: float = Field(ge=0, le=1)
    sigma_gap_max: float
    metrics: tuple[str, ...]

    @field_validator("metrics", mode="before")
    @classmethod
    def _tuple_metrics(cls, value: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        return tuple(value)

    @field_validator("excluded_sectors", mode="before")
    @classmethod
    def _tuple_excluded_sectors(cls, value: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
        return tuple(value or ())


class CashRichEvidencePattern(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    evidence_pattern_id: str
    excluded_sectors: tuple[str, ...] = ()
    cash_to_market_cap_min: float = Field(ge=0)
    edinet_net_cash_to_market_cap_min_if_available: float | None = None
    pbr_max: float = Field(ge=0)
    equity_ratio_min: float = Field(ge=0, le=1)
    operating_profit_positive_required: bool
    operating_profit_yoy_deterioration_threshold: float | None = None

    @field_validator("excluded_sectors", mode="before")
    @classmethod
    def _tuple_excluded_sectors(cls, value: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
        return tuple(value or ())


class CashflowYieldEvidencePattern(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    evidence_pattern_id: str
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


class SalesDiscountGrowthEvidencePattern(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    evidence_pattern_id: str
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
    evidence_pattern_order: tuple[str, ...]

    @field_validator("evidence_pattern_order", mode="before")
    @classmethod
    def _tuple_evidence_pattern_order(cls, value: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        return tuple(value)


class DurabilityDiagnosticRules(BaseModel):
    """塩漬け耐性 (durability) annotation の事前固定閾値。

    価格 stop を置かない long-hold の前提を成立させる耐性シグナル
    (balance sheet・現金・CF・流動性) を candidates に注記する。
    ranking / gate には使わず、research の必須ゲート判定の機械入力になる。
    """

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


class CandidateDiagnosticRules(BaseModel):
    """Thresholds for shared annotations that never nominate or order candidates."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    durability: DurabilityDiagnosticRules = Field(default_factory=DurabilityDiagnosticRules)


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
        """Single predicate for the investable set.

        ``require_facts=True`` disqualifies rows with missing liquidity facts.
        Selection uses this mode so the ranked population matches the
        calibration population; diagnostics separately count missing facts.
        """
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

    liquidity: SelectionLiquidityRules = Field(default_factory=SelectionLiquidityRules)
    candidate_diagnostics: CandidateDiagnosticRules = Field(
        default_factory=CandidateDiagnosticRules
    )


class ScreeningRules(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    universe: UniverseRules
    ttm: TTMRules
    quality: QualityRules
    evidence_patterns: Mapping[
        str,
        ValuationReversionEvidencePattern
        | CashRichEvidencePattern
        | CashflowYieldEvidencePattern
        | SalesDiscountGrowthEvidencePattern,
    ]
    output: OutputRules
    selection: SelectionRules = Field(default_factory=SelectionRules)

    @field_validator("evidence_patterns", mode="before")
    @classmethod
    def _coerce_evidence_patterns(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError("evidence_patterns must be a mapping")
        patterns: dict[str, Any] = {}
        for name, raw in value.items():
            if not isinstance(raw, Mapping):
                raise ValueError(f"evidence pattern {name!r} must be a mapping")
            data = dict(raw)
            match name:
                case "valuation-reversion":
                    patterns[name] = ValuationReversionEvidencePattern.model_validate(data)
                case "cash-rich-asset-discount":
                    patterns[name] = CashRichEvidencePattern.model_validate(data)
                case "cashflow-yield-discount":
                    patterns[name] = CashflowYieldEvidencePattern.model_validate(data)
                case "sales-discount-growth":
                    patterns[name] = SalesDiscountGrowthEvidencePattern.model_validate(data)
                case _:
                    raise ValueError(f"unknown evidence pattern: {name}")
        return patterns

    @property
    def evidence_pattern_order(self) -> tuple[str, ...]:
        return tuple(self.evidence_patterns.keys())

    @model_validator(mode="after")
    def _validate_output_evidence_pattern_order(self) -> ScreeningRules:
        unknown = set(self.output.evidence_pattern_order) - set(self.evidence_patterns)
        if unknown:
            joined = ", ".join(sorted(unknown))
            raise ValueError(f"unknown Evidence Pattern(s): {joined}")
        return self


def load_screening_rules(path: Path = DEFAULT_RULES_PATH) -> ScreeningRules:
    if not path.exists() and not path.is_absolute():
        path = Path(__file__).resolve().parents[4] / path
    payload = safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"screening rules root must be a mapping: {path}")
    return ScreeningRules.model_validate(payload)
