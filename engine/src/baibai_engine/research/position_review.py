"""保有判断工程で企業評価と提出された残存見返りから hold / exit を産む。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from baibai_engine.foundation.time import JST
from baibai_engine.research.thesis import ThesisDocument, current_price_projection
from baibai_engine.valuation.scenario import finite_decimal

_CONFIG = ConfigDict(frozen=True, strict=True, extra="forbid", allow_inf_nan=False)


class RemainingReward(BaseModel):
    model_config = _CONFIG
    status: Literal["sufficient", "insufficient", "uncertain"]
    reason: Annotated[str, Field(min_length=1, pattern=r"\S")]


class HoldingInput(BaseModel):
    model_config = _CONFIG
    quantity: Annotated[int, Field(gt=0)]
    cost_yen: Annotated[Decimal, Field(ge=0)]
    quantity_basis_confirmed: bool

    @field_validator("cost_yen", mode="before")
    @classmethod
    def _number(cls, value: object) -> Decimal:
        return finite_decimal(value)


class QuoteInput(BaseModel):
    model_config = _CONFIG
    price_yen: Annotated[Decimal, Field(gt=0)]
    observed_at: datetime
    price_basis: Literal["last_close_unadjusted", "realtime"]
    source_ref: Annotated[str, Field(min_length=1)]
    basis_confirmed: bool

    @field_validator("price_yen", mode="before")
    @classmethod
    def _number(cls, value: object) -> Decimal:
        return finite_decimal(value)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _time(cls, value: object) -> datetime:
        value = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("quote time requires a timezone")
        return value


class PositionReviewDocument(BaseModel):
    model_config = _CONFIG
    schema_version: Literal[3]
    position_review_id: Annotated[str, Field(min_length=1)]
    thesis_id: Annotated[str, Field(min_length=1)]
    position_id: Annotated[str, Field(min_length=1)]
    ticker: Annotated[str, Field(pattern=r"^[0-9A-Z]{4}$")]
    as_of: date
    holding: HoldingInput
    quote: QuoteInput | None
    remaining_reward: RemainingReward | None
    action: Literal["hold", "exit"] | None = None
    unresolved_reason: str | None = None

    @field_validator("as_of", mode="before")
    @classmethod
    def _date(cls, value: object) -> object:
        return date.fromisoformat(value) if isinstance(value, str) else value


@dataclass(frozen=True, slots=True)
class PositionReviewEvaluation:
    action: Literal["hold", "exit"] | None
    reason: str
    sell_quantity: int | None
    current_price_projection: dict[str, object] | None = None


def evaluate_position_review(
    document: PositionReviewDocument,
    thesis: ThesisDocument,
    *,
    primary_verified: bool,
    max_quote_age_days: int,
) -> PositionReviewEvaluation:
    """Economic invalidation precedes valuation; buy floors never select an exit."""
    if thesis.input_snapshot.ticker != document.ticker:
        raise ValueError("holding and Thesis ticker differ")
    case = thesis.investment_case
    if case.status == "broken" and primary_verified:
        return PositionReviewEvaluation(
            "exit",
            "economic_invalidation",
            document.holding.quantity if document.holding.quantity_basis_confirmed else None,
        )
    quote = document.quote
    quote_as_of = None if quote is None else quote.observed_at.astimezone(JST).date()
    quote_age_days = None if quote_as_of is None else (document.as_of - quote_as_of).days
    unavailable_reason: str | None = None
    if case.status != "intact":
        unavailable_reason = "investment_case_uncertain"
    elif not primary_verified:
        unavailable_reason = "primary_evidence_requires_verification"
    elif thesis.valuation.status != "resolved":
        unavailable_reason = "valuation_unresolved"
    elif thesis.input_snapshot.as_of != document.as_of:
        unavailable_reason = "valuation_basis_requires_refresh"
    elif quote is None:
        unavailable_reason = "quote_missing"
    elif not document.holding.quantity_basis_confirmed or not quote.basis_confirmed:
        unavailable_reason = "quantity_basis_unconfirmed"
    elif quote_age_days is not None and quote_age_days < 0:
        unavailable_reason = "quote_after_judgment"
    elif quote_age_days is not None and quote_age_days > max_quote_age_days:
        unavailable_reason = "quote_stale"
    available = unavailable_reason is None
    projection = current_price_projection(
        thesis,
        price_yen=None if quote is None else quote.price_yen,
        price_as_of=quote_as_of,
        as_of=document.as_of,
        basis_confirmed=available,
        max_quote_age_days=max_quote_age_days,
        price_basis="last_close_unadjusted" if quote is None else quote.price_basis,
    )
    if (
        not available
        or document.remaining_reward is None
        or document.remaining_reward.status == "uncertain"
    ):
        return PositionReviewEvaluation(
            None,
            unavailable_reason
            or document.unresolved_reason
            or "remaining_reward_requires_confirmation",
            None,
            projection,
        )
    if document.remaining_reward.status == "sufficient":
        return PositionReviewEvaluation("hold", "sufficient_remaining_reward", None, projection)
    return PositionReviewEvaluation(
        "exit", "insufficient_remaining_reward", document.holding.quantity, projection
    )
