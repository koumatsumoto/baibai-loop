from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import isfinite
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from baibai_engine.machine_analysis.models.company_financials.metrics.models import TTMRules
from baibai_engine.primitives.yaml import safe_load
from baibai_engine.storage.paths import SCREENING_RULES_PATH

DEFAULT_RULES_PATH = SCREENING_RULES_PATH


class UniverseRules(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    required_jpx_flags: tuple[str, ...]

    @field_validator("required_jpx_flags", mode="before")
    @classmethod
    def _tuple_flags(cls, value: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        return tuple(value)


class QualityRules(BaseModel):
    """旧rulesのhash・較正identityに残す品質設定。現行モデルのgateではない。"""

    model_config = ConfigDict(frozen=True, strict=True)

    partial_warning_ttm_ratio: float = Field(ge=0)
    partial_warning_ttm_count: int = Field(ge=0)
    partial_warning_yoy_missing_ratio: float = Field(ge=0)


class CommonEligibilityRules(BaseModel):
    """Analysis-layer scope parameters applied when ranking research candidates.

    The former Stock Analysis screen evaluates common stocks with usable prices. This
    predicate shares valid scope facts and configured exclusions between
    Candidate Discovery and its comparison population.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    min_market_cap_oku: float = Field(default=0, ge=0)
    # IPOの価格履歴は必要財務履歴ではない。期間をcontextへ残す (#1481)。
    # docs/reference/screening-runtime.md#保存と読取
    min_price_history_span_days: int = Field(default=0, ge=0)
    exclude_jpx_flagged: bool = True

    def matches(
        self,
        *,
        market_cap_oku: float | None,
        price_history_span_days: float | None,
        jpx_flags: Sequence[str] | None,
        required_jpx_flags: frozenset[str],
        require_facts: bool,
    ) -> bool:
        """Single predicate for the Candidate Discovery common eligible set.

        ``require_facts=True`` disqualifies rows with missing scope facts.
        Candidate Discovery uses this mode so the eligible population matches the
        calibration population; diagnostics separately count missing facts.
        欠測を確認済みの無規制・正の尺度へ読み替えない:
        docs/reference/screening-runtime.md#保存と読取
        """
        facts = (market_cap_oku, price_history_span_days, jpx_flags)
        if require_facts and any(value is None for value in facts):
            return False
        if market_cap_oku is not None and (not isfinite(market_cap_oku) or market_cap_oku <= 0):
            return False
        if price_history_span_days is not None and not isfinite(price_history_span_days):
            return False
        if market_cap_oku is not None and market_cap_oku < self.min_market_cap_oku:
            return False
        if (
            price_history_span_days is not None
            and price_history_span_days < self.min_price_history_span_days
        ):
            return False
        return not (
            self.exclude_jpx_flagged and jpx_flags and required_jpx_flags.intersection(jpx_flags)
        )


class ValuationApproachRules(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    method_id: str = Field(min_length=1)


class CandidateDiscoveryRules(BaseModel):
    """The explicit multi-valuation method that produces finite candidate contexts."""

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
        path = Path(__file__).resolve().parents[6] / path
    payload = safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"screening rules root must be a mapping: {path}")
    return ScreeningRules.model_validate(payload)
