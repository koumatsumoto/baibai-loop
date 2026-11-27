"""認証済みWebに渡す一件のUSD判断。判断・執行条件は作らない。"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel


class USProposalOfficialCloseView(BaseModel):
    price_usd: Decimal
    origin: Literal["listing_exchange"]
    observed_at: datetime


class USProposalIexBandView(BaseModel):
    first_session_et: date
    last_session_et: date
    observations: int
    low_usd: Decimal
    high_usd: Decimal
    last_usd: Decimal


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
    official_close: USProposalOfficialCloseView | None
    iex_band: USProposalIexBandView | None
    comparison_horizon_et: date | None
    unresolved: list[str]


class LatestUSProposalView(BaseModel):
    generated_at: datetime
    status: Literal["ready", "empty", "unavailable"]
    error: str | None
    proposal: USProposalView | None
