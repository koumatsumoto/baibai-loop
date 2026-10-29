"""保存・公開query・投資判断が同じ形で使う企業成長の入力契約。"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

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


class Value(Contract):
    value: FiniteFloat | None = None
    reason: str | None = None
    documents: tuple[SourceDocument, ...] = ()


class Observation(Contract):
    period_start: date
    period_end: date
    measurement: Measurement
    statement_basis: str
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
    calculation_method: str = "company_growth_retrospective_v6"
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
    per: OptionalContext = OptionalContext(
        reason="price_share_unit_and_parent_profit_alignment_not_verified"
    )
    peer: OptionalContext = OptionalContext(
        reason="fixed_peer_group_and_comparable_pair_inputs_not_selected"
    )
