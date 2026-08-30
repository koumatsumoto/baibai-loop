"""Canonical comparison of research-ready investment alternatives.

The assessment owns the allocation judgment and prose. Financial estimates,
fair value, and permanent-loss facts stay authoritative in the bound immutable
Investment Thesis and are derived only by read surfaces.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from hashlib import sha256
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from baibai_engine.appdb.json import canonical_json

CAPITAL_ALLOCATION_ASSESSMENT_SCHEMA_VERSION = 1

type AllocationResult = Literal["allocate", "no_allocation", "defer"]
type AlternativeDisposition = Literal["allocate", "decline", "defer"]


class CapitalAllocationError(ValueError):
    """Raised when an assessment cannot be validated or published."""


class CapitalAllocationConflictError(CapitalAllocationError):
    """Raised when immutable identities or source bindings differ."""


class AllocationAlternative(BaseModel):
    """One researched alternative, bound to an immutable reviewed thesis."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ticker: str = Field(pattern=r"^[0-9A-Z]{4}$")
    thesis_id: str = Field(min_length=1)
    thesis_core_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    thesis_review_id: str | None = None
    disposition: AlternativeDisposition
    rationale: str = Field(min_length=1)


class ContentReviewBinding(BaseModel):
    """Independent content review bound to the exact assessment draft."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    attempt: int = Field(ge=1)
    reviewer_identity: str = Field(min_length=1)
    reviewed_at: datetime
    conclusion: Literal["pass"]
    draft_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    open_findings: tuple[str, ...] = ()

    @field_validator("open_findings", mode="before")
    @classmethod
    def _tuple_open_findings(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("reviewed_at", mode="before")
    @classmethod
    def _parse_reviewed_at(cls, value: object) -> object:
        return datetime.fromisoformat(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def _timezone_required(self) -> Self:
        if self.reviewed_at.tzinfo is None:
            raise ValueError("reviewed_at must include a timezone")
        if self.open_findings:
            raise ValueError("a passing review cannot carry open findings")
        return self


class CapitalAllocationAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    kind: Literal["capital_allocation_assessment"]
    capital_allocation_assessment_id: str = Field(min_length=1)
    as_of: date
    published_at: datetime
    result: AllocationResult
    headline: str = Field(min_length=1)
    research_triage_id: str = Field(min_length=1)
    macro_context_id: str | None = None
    comparison: str = Field(min_length=1)
    forgone: str = Field(min_length=1)
    alternatives: tuple[AllocationAlternative, ...] = Field(min_length=1)
    review: ContentReviewBinding

    @field_validator("alternatives", mode="before")
    @classmethod
    def _tuple_alternatives(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("as_of", mode="before")
    @classmethod
    def _parse_as_of(cls, value: object) -> object:
        return date.fromisoformat(value) if isinstance(value, str) else value

    @field_validator("published_at", mode="before")
    @classmethod
    def _parse_published_at(cls, value: object) -> object:
        return datetime.fromisoformat(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def _publication_shape(self) -> Self:
        if self.published_at.tzinfo is None:
            raise ValueError("published_at must include a timezone")
        if not re.fullmatch(
            r"(?:capital-allocation-assessment|bargain-assessment)-\d{8}-[a-z0-9-]+",
            self.capital_allocation_assessment_id,
        ):
            raise ValueError("capital allocation assessment ID has an invalid format")
        tickers = [alternative.ticker for alternative in self.alternatives]
        if len(tickers) != len(set(tickers)):
            raise ValueError("alternatives must have unique tickers")
        allocated = [item for item in self.alternatives if item.disposition == "allocate"]
        if self.result == "allocate":
            if len(allocated) != 1:
                raise ValueError("allocate requires exactly one allocated alternative")
            if allocated[0].thesis_review_id is None:
                raise ValueError("allocated alternative requires an independent review")
        elif allocated:
            raise ValueError("only allocate may carry an allocated alternative")
        return self

    def payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def capital_allocation_draft_sha256(assessment: CapitalAllocationAssessment) -> str:
    """Hash the reviewed judgment, excluding its review and publication time."""

    payload = assessment.payload()
    payload.pop("review", None)
    payload.pop("published_at", None)
    return sha256(canonical_json(payload).encode()).hexdigest()


__all__ = [
    "CAPITAL_ALLOCATION_ASSESSMENT_SCHEMA_VERSION",
    "AllocationAlternative",
    "AllocationResult",
    "AlternativeDisposition",
    "CapitalAllocationAssessment",
    "CapitalAllocationConflictError",
    "CapitalAllocationError",
    "ContentReviewBinding",
    "capital_allocation_draft_sha256",
]
