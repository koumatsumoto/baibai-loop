"""Stock Analysis candidate values and the frozen discovery algorithm identity."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

type ValuationApproachId = Literal[
    "current-earnings-power",
    "normalized-earnings-power",
    "asset-value",
    "reinvestment-value",
]

APPROACH_IDS: tuple[ValuationApproachId, ...] = (
    "current-earnings-power",
    "normalized-earnings-power",
    "asset-value",
    "reinvestment-value",
)


class CandidateReason(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    valuation_approach_id: ValuationApproachId
    valuation_method_id: str = Field(min_length=1)
    rank: int = Field(ge=1)


class StockCandidateContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    common_eligible: bool
    reasons: tuple[CandidateReason, ...]
    normalized_per_3fy_sector_gap: float | None

    @field_validator("reasons", mode="before")
    @classmethod
    def _tuple_reasons(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("normalized_per_3fy_sector_gap")
    @classmethod
    def _finite_gap(cls, value: float | None) -> float | None:
        if value is not None and not isfinite(value):
            raise ValueError("normalized candidate gap must be finite")
        return value

    @model_validator(mode="after")
    def _consistent_context(self) -> Self:
        approaches = [reason.valuation_approach_id for reason in self.reasons]
        if len(set(approaches)) != len(approaches) or approaches != sorted(
            approaches, key=APPROACH_IDS.index
        ):
            raise ValueError("candidate reasons must be unique and in Approach order")
        if not self.common_eligible and (
            self.reasons or self.normalized_per_3fy_sector_gap is not None
        ):
            raise ValueError("ineligible analysis cannot have candidate reasons or gap")
        return self


class CandidateDiscoveryMethodIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    method_id: str = Field(min_length=1)
    method_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Existing rules and calibration hash wire; no separate Nomination publication.
    nomination_depth: int = Field(gt=0)


class CandidateDiscoveryDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    common_eligible_count: int = Field(ge=0)
    candidate_counts_by_approach: dict[ValuationApproachId, int]
    candidate_count: int = Field(ge=0)

    @field_validator("candidate_counts_by_approach")
    @classmethod
    def _complete_counts(
        cls, value: dict[ValuationApproachId, int]
    ) -> dict[ValuationApproachId, int]:
        if set(value) != set(APPROACH_IDS) or any(count < 0 for count in value.values()):
            raise ValueError("candidate counts must cover all four Approaches")
        return value


class CandidateDiscoverySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    method: CandidateDiscoveryMethodIdentity
    diagnostics: CandidateDiscoveryDiagnostics


@dataclass(frozen=True, slots=True)
class CandidateDiscoveryResult:
    """Pure calculation result; contexts become rows, diagnostics become one header."""

    method: CandidateDiscoveryMethodIdentity
    contexts: Mapping[str, StockCandidateContext]
    common_eligible_count: int
    candidate_counts_by_approach: Mapping[str, int]
    candidate_count: int

    def summary_payload(self) -> dict[str, object]:
        return {
            "method": self.method.model_dump(mode="json"),
            "diagnostics": {
                "common_eligible_count": self.common_eligible_count,
                "candidate_counts_by_approach": dict(self.candidate_counts_by_approach),
                "candidate_count": self.candidate_count,
            },
        }


def validate_candidate_run(
    header: Mapping[str, object], rows: Sequence[Mapping[str, object]]
) -> None:
    """Validate a stored run without consulting today's rules or recomputing values."""
    if type(header.get("schema_version")) is not int or header["schema_version"] != 1:
        raise ValueError("stock analysis run schema_version must be 1")
    if header.get("kind") != "stock_analysis_run":
        raise ValueError("run kind must be stock_analysis_run")
    summary = CandidateDiscoverySummary.model_validate(header.get("candidate_discovery"))
    tickers: set[str] = set()
    ranks: dict[ValuationApproachId, list[int]] = {approach: [] for approach in APPROACH_IDS}
    common_eligible_count = 0
    candidate_count = 0
    for row in rows:
        ticker = row.get("ticker")
        if not isinstance(ticker, str) or ticker in tickers:
            raise ValueError("stock analysis tickers must be unique")
        tickers.add(ticker)
        context = StockCandidateContext.model_validate(row.get("candidate"))
        common_eligible_count += context.common_eligible
        candidate_count += bool(context.reasons)
        for reason in context.reasons:
            ranks[reason.valuation_approach_id].append(reason.rank)
    for approach, values in ranks.items():
        if sorted(values) != list(range(1, len(values) + 1)):
            raise ValueError(f"{approach} candidate ranks must be contiguous from 1")
        if len(values) > summary.method.nomination_depth:
            raise ValueError(f"{approach} exceeds nomination_depth")
    if candidate_count > len(APPROACH_IDS) * summary.method.nomination_depth:
        raise ValueError("candidate union exceeds the configured depth")
    actual = CandidateDiscoveryDiagnostics(
        common_eligible_count=common_eligible_count,
        candidate_count=candidate_count,
        candidate_counts_by_approach={approach: len(values) for approach, values in ranks.items()},
    )
    if actual != summary.diagnostics:
        raise ValueError("candidate diagnostics differ from stock analysis rows")


__all__ = [
    "APPROACH_IDS",
    "CandidateDiscoveryDiagnostics",
    "CandidateDiscoveryMethodIdentity",
    "CandidateDiscoveryResult",
    "CandidateDiscoverySummary",
    "CandidateReason",
    "StockCandidateContext",
    "ValuationApproachId",
    "validate_candidate_run",
]
