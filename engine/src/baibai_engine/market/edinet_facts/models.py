"""Typed rows; each row represents one source observation, never an estimate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, FiniteFloat


class FactIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    source_doc_id: str
    source_submit_datetime: str
    disclosed_on: str


class SegmentFact(FactIdentity):
    source_element: str
    source_context: str
    issuer_id: str
    period_start: str | None
    period_end: str
    consolidation_basis: str
    segment_axis: str
    segment_key: str
    segment_name: str | None
    segment_kind: str
    metric: str
    profit_basis: str | None
    value: FiniteFloat | None
    currency: str
    source_locator: str


class DebtFact(FactIdentity):
    source_element: str
    source_context: str
    issuer_id: str
    balance_sheet_date: str
    consolidation_basis: str
    debt_category: str
    due_from_months: int
    due_to_months: int
    principal: FiniteFloat | None
    currency: str
    source_locator: str


@dataclass(frozen=True)
class ExtractedFacts:
    segments: tuple[SegmentFact, ...]
    debt: tuple[DebtFact, ...]
    segment_reasons: tuple[str, ...] = ()
    debt_reasons: tuple[str, ...] = ()


def filing_identity(ticker: str, doc_id: str, submitted: str) -> FactIdentity:
    stamp = datetime.fromisoformat(submitted)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=ZoneInfo("Asia/Tokyo"))
    stamp = stamp.astimezone(ZoneInfo("Asia/Tokyo"))
    return FactIdentity(
        ticker=ticker,
        source_doc_id=doc_id,
        source_submit_datetime=stamp.isoformat(),
        disclosed_on=stamp.date().isoformat(),
    )
