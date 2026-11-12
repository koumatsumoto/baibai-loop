"""Pure same-document comparison inputs; native decoding is a separate owner."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

REVISION = "edinet-ifrs-statement-comparisons-v2-consolidated-annual"
type ComparativeMetric = Literal["sales", "operating_profit", "parent_profit"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NativeAmount(Contract):
    # Decimal text and source precision are evidence; model floats never replace them.
    amount: str | None
    decimals: str | None
    concept: str
    context_id: str
    issuer_id: str
    period_start: date
    period_end: date
    unit_ref: str
    currency: Literal["JPY"] = "JPY"
    instance_entry: str
    presentation_entry: str
    presentation_role: str
    locators: tuple[str, ...]


class ComparativeOperands(Contract):
    metric: ComparativeMetric
    current: NativeAmount | None = None
    comparative: NativeAmount | None = None
    reason: str | None = None


class FilingComparisons(Contract):
    ticker: str
    source_doc_id: str
    source_submit_datetime: datetime
    document_type: str
    parent_doc_id: str | None = None
    source_sha256: str | None = None
    extractor_revision: str = REVISION
    accounting_standard: Literal["IFRS"] | None = None
    statement_basis: Literal["consolidated", "nonconsolidated"] | None = None
    current_period_start: date | None = None
    current_period_end: date | None = None
    pairs: tuple[ComparativeOperands, ...] = ()
    reason: str | None = None
