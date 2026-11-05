"""独立した検証者が記録した反証と再計算。元の評価を上書きしない。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AliasChoices, AwareDatetime, BaseModel, Field, field_validator

from baibai_engine.investment_judgment.contracts import (
    _CONFIG,
    AssessmentReviewId,
    NonEmptyStr,
    _datetime,
    _tuple,
)
from baibai_engine.primitives.numbers import finite_decimal


class ReviewedProjection(BaseModel):
    model_config = _CONFIG
    name: Literal["base", "downside"]
    terminal_value_per_share_yen: Annotated[Decimal, Field(ge=0)]
    cash_distribution_per_share_yen: Annotated[Decimal, Field(ge=0)]

    @field_validator(
        "terminal_value_per_share_yen", "cash_distribution_per_share_yen", mode="before"
    )
    @classmethod
    def _number(cls, value: object) -> Decimal:
        return finite_decimal(value)


class AssessmentReview(BaseModel):
    model_config = _CONFIG
    review_id: AssessmentReviewId
    reviewer_role: Literal["independent_second_pass"]
    reviewer_identity: NonEmptyStr
    reviewed_at: AwareDatetime
    reviewed_assessment_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    primary_source_check: Literal["verified", "partially_verified", "unverified"]
    checked_source_ids: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]
    recalculated_projections: tuple[ReviewedProjection, ...]
    strongest_countercase: NonEmptyStr
    uncertainty_acceptance_reason: NonEmptyStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "uncertainty_acceptance_reason", "nonmaterial_unknown_reason"
        ),
    )
    review_scope: NonEmptyStr

    @field_validator("reviewed_at", mode="before")
    @classmethod
    def _time(cls, value: object) -> datetime:
        return _datetime(value)

    @field_validator("checked_source_ids", "recalculated_projections", mode="before")
    @classmethod
    def _sequences(cls, value: object) -> object:
        return _tuple(value)
