from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

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


class CommonEligibilityRules(BaseModel):
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
        Candidate Discovery uses this mode so the eligible population matches the
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


class ValuationApproachRules(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    method_id: str = Field(min_length=1)


class CandidateDiscoveryRules(BaseModel):
    """The explicit multi-valuation method that produces the finite Review Set."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    method_id: str = Field(min_length=1)
    nomination_depth: int = Field(gt=0)
    common_eligibility: CommonEligibilityRules
    approaches: Mapping[str, ValuationApproachRules]

    @model_validator(mode="after")
    def _validate_method(self) -> CandidateDiscoveryRules:
        expected = {
            "current-earnings-power",
            "normalized-earnings-power",
            "asset-value",
            "reinvestment-value",
        }
        if set(self.approaches) != expected:
            raise ValueError("candidate discovery must define the four valuation approaches")
        return self


class ScreeningRules(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    universe: UniverseRules
    ttm: TTMRules
    quality: QualityRules
    candidate_discovery: CandidateDiscoveryRules


def load_screening_rules(path: Path = DEFAULT_RULES_PATH) -> ScreeningRules:
    if not path.exists() and not path.is_absolute():
        path = Path(__file__).resolve().parents[4] / path
    payload = safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"screening rules root must be a mapping: {path}")
    return ScreeningRules.model_validate(payload)
