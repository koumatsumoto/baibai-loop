"""認証済みWebに渡す保存済みUSD判断。判断や執行条件は作らない。"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, WithJsonSchema

from baibai_web.readmodel.perspective_vocabulary import PerspectiveAssessment, PerspectiveModel


class USProposalOfficialCloseView(BaseModel):
    price_usd: Decimal
    origin: Literal["listing_exchange", "alpaca_sip_nasdaq_official_close"]
    observed_at: datetime


class USProposalIexBandView(BaseModel):
    first_session_et: date
    last_session_et: date
    observations: int
    low_usd: Decimal
    high_usd: Decimal
    last_usd: Decimal


# Decimal JSON can use exponents for small exact values. Describe that output
# instead of rounding or changing its precision to fit the default Decimal schema.
type USProposalComparisonAmount = Annotated[
    Decimal,
    WithJsonSchema(
        {"type": "string", "pattern": r"^-?\d+(?:\.\d+)?(?:E[+-]?\d+)?$"},
        mode="serialization",
    ),
]


class USProposalComparisonScenarioView(BaseModel):
    terminal_value_usd_per_share: USProposalComparisonAmount
    eligible_distributions_usd_per_share: USProposalComparisonAmount
    difference_from_cash_usd_per_share: USProposalComparisonAmount


class USProposalComparisonView(BaseModel):
    cash_basis: Literal["same_usd_no_interest_tax_or_fees"]
    base: USProposalComparisonScenarioView
    downside: USProposalComparisonScenarioView


class USProposalPerspectiveSourceView(BaseModel):
    label: str
    observed_at: datetime


class USProposalPerspectiveView(BaseModel):
    model: PerspectiveModel
    assessment: PerspectiveAssessment
    summary: str
    sources: list[USProposalPerspectiveSourceView]


class USProposalView(BaseModel):
    proposal_id: str
    draft_sha256: str
    ticker: str
    venue: Literal["XNAS", "XNYS"]
    share_class: str
    currency: Literal["USD"]
    session_et: date
    cutoff_at: datetime
    published_at: datetime
    reviewed_at: datetime
    action: Literal["defer", "no_action"]
    reason: str
    revisit_when: str
    model_perspectives: list[USProposalPerspectiveView]
    official_close: USProposalOfficialCloseView | None
    iex_band: USProposalIexBandView | None
    comparison_horizon_et: date | None
    comparison: USProposalComparisonView | None
    unresolved: list[str]


class USProposalHistoryView(BaseModel):
    """Recent publication slots. The readable latest is carried only at the root."""

    status: Literal["ready", "empty", "partial", "unavailable"]
    error: str | None
    proposals: list[USProposalView]
    unavailable_proposal_ids: list[str]
    has_more: bool | None


class LatestUSProposalView(BaseModel):
    generated_at: datetime
    status: Literal["ready", "empty", "unavailable"]
    error: str | None
    proposal: USProposalView | None
    history: USProposalHistoryView
