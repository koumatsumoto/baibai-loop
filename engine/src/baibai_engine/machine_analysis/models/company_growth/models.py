"""保存・公開query・投資判断が同じ形で使う企業成長の入力契約。"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from baibai_engine.machine_analysis.observations.financials.edinet_comparisons import (
    FilingComparisons,
)
from baibai_engine.machine_analysis.observations.financials.pdf_selection import (
    PdfFinancialSelection,
)
from baibai_engine.machine_analysis.observations.financials.pdf_statements import PdfSupplement

type Metric = Literal[
    "sales",
    "operating_profit",
    "parent_profit",
    "operating_margin",
    "reported_basic_eps",
    "average_shares",
    "issued_shares",
    "treasury_shares",
    "operating_cash_flow",
    "net_assets",
    "total_assets",
]
type Measurement = Literal["reported", "quarter", "annual_ttm", "balance"]
type PeriodStatus = Literal[
    "available", "matching_period_not_observed", "accounting_periods_incomparable"
]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceDocument(Contract):
    disclosure_number: str | None
    disclosed_at: date
    disclosed_time: str | None
    document_type: str | None
    source_url: str | None = Field(default=None, exclude_if=lambda value: value is None)
    source_sha256: str | None = Field(default=None, exclude_if=lambda value: value is None)


class Value(Contract):
    precision: Literal["published_display_amounts", "derived_from_truncated_display_amounts"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    value: FiniteFloat | None = None
    reason: str | None = None
    documents: tuple[SourceDocument, ...] = ()


class Observation(Contract):
    period_start: date
    period_end: date
    measurement: Measurement
    statement_basis: str
    # Absent in old saved generations; do not synthesize it when reading them.
    accounting_standard: str | None = Field(default=None, exclude_if=lambda value: value is None)
    # Interval construction is independent of missing/conflicting metric operands.
    period_status: PeriodStatus | None = Field(default=None, exclude_if=lambda value: value is None)
    # Reported EPS and share counts retain each document's native share basis.
    share_basis: Literal["source_reported_unverified"] = "source_reported_unverified"
    metrics: dict[Metric, Value]


class PeriodState(Contract):
    fiscal_period: str | None
    fiscal_year_end: date | None
    period_start: date
    period_end: date
    observations: tuple[Observation, ...]


class Change(Contract):
    precision: Literal["published_display_amounts", "derived_from_truncated_display_amounts"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    source_sha256: str | None = Field(default=None, exclude_if=lambda value: value is None)
    metric: Metric
    measurement: Measurement
    comparison: Literal["year_over_year"] = "year_over_year"
    from_start: date | None = None
    from_end: date | None = None
    to_start: date
    to_end: date
    from_value: FiniteFloat | None = None
    to_value: FiniteFloat | None = None
    difference: FiniteFloat | None = None
    rate: FiniteFloat | None = None
    margin_points: FiniteFloat | None = None
    sign_change: str | None = None
    reason: str | None = None


class InputRelease(Contract):
    release_id: str
    manifest_sha256: str
    collection_boundary: Literal["fixed_release_membership"] = "fixed_release_membership"
    # The manifest pins membership; local coverage does not prove receipt timestamps.
    receipt_cutoff: None = None


class ReportedComparison(Contract):
    observation: FilingComparisons
    changes: tuple[Change, ...] = ()
    read_semantics: Literal["each_document_vintage_not_latest_or_ttm"] = (
        "each_document_vintage_not_latest_or_ttm"
    )


class OptionalContext(Contract):
    status: Literal["not_provided"] = "not_provided"
    reason: str


class Coverage(Contract):
    status: Literal["unverified"] = "unverified"
    reason: str = "store_local_acquisition_coverage_is_not_bound_to_fixed_release"
    observed_start: date | None = None
    observed_end: date | None = None
    observed_period_ends: tuple[date, ...] = ()
    # These are observed gaps, not claims about provider completeness.
    gaps_between_observations: tuple[tuple[date, date], ...] = ()
    excluded_document_count: int = 0


class CompanyGrowth(Contract):
    ticker: str
    status: Literal["available", "unavailable"]
    reason: str | None = None
    calculation_method: str = "company_growth_retrospective_v7"
    read_semantics: Literal["retained_latest_in_fixed_release_not_point_in_time"] = (
        "retained_latest_in_fixed_release_not_point_in_time"
    )
    comparison_basis: Literal["source_reported_economic_scope_unverified"] = (
        "source_reported_economic_scope_unverified"
    )
    metric_units: dict[Metric, str] = {
        "sales": "JPY",
        "operating_profit": "JPY",
        "parent_profit": "JPY",
        "operating_margin": "ratio",
        "reported_basic_eps": "JPY_per_source_reported_share",
        "average_shares": "shares",
        "issued_shares": "shares",
        "treasury_shares": "shares",
        "operating_cash_flow": "JPY",
        "net_assets": "JPY",
        "total_assets": "JPY",
    }
    requested_start: date | None
    requested_end: date
    input_release: InputRelease | None = None
    coverage: Coverage = Coverage()
    periods: tuple[PeriodState, ...] = ()
    changes: tuple[Change, ...] = ()
    reported_comparisons: tuple[ReportedComparison, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    comparison_input_reason: str | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    pdf_selection: PdfFinancialSelection | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    pdf_supplement: PdfSupplement | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    pdf_input_reason: str | None = Field(default=None, exclude_if=lambda value: value is None)
    per: OptionalContext = OptionalContext(
        reason="price_share_unit_and_parent_profit_alignment_not_verified"
    )
    peer: OptionalContext = OptionalContext(
        reason="fixed_peer_group_and_comparable_pair_inputs_not_selected"
    )
