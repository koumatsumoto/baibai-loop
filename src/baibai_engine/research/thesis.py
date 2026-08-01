"""Canonical long-horizon investment thesis and deterministic checks."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, DecimalException, InvalidOperation, localcontext
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from baibai_engine.foundation.yaml_io import safe_load

_CONFIG = ConfigDict(frozen=True, strict=True, extra="forbid", allow_inf_nan=False)
_TICKER = r"^[0-9A-Z]{4}$"
_RISK_AXES = frozenset(
    {
        "funding_liquidity",
        "debt_repayment",
        "cash_flow",
        "dilution",
        "customer_concentration",
        "structural_decline",
        "governance_accounting",
    }
)
_SCENARIO_KEYS = frozenset(
    (horizon, name) for horizon in (3, 5) for name in ("bear", "base", "bull")
)
_SCENARIO_ORDER = {"bear": 0, "base": 1, "bull": 2}
_INCOMPLETE_EVIDENCE_OVERRIDE_REQUIRED = (
    "buy with incomplete or adverse evidence requires a human override and reduced sizing"
)
_PRIMARY_REVIEW_OVERRIDE_REQUIRED = (
    "buy without fully verified primary review requires an active override and reduced sizing"
)
_EXPIRY_ONLY_ERRORS = frozenset(
    {_INCOMPLETE_EVIDENCE_OVERRIDE_REQUIRED, _PRIMARY_REVIEW_OVERRIDE_REQUIRED}
)
RiskAxis = Literal[
    "funding_liquidity",
    "debt_repayment",
    "cash_flow",
    "dilution",
    "customer_concentration",
    "structural_decline",
    "governance_accounting",
]
TerminalMultipleStatus = Literal[
    "within_model_bounds",
    "below_model_min",
    "above_model_max",
    "dividends_alone_sufficient",
    "calculation_unresolved",
]
EarningsGrowthStatus = Literal[
    "within_model_bounds",
    "below_model_min",
    "above_model_max",
    "dividends_alone_sufficient",
    "calculation_unresolved",
]
ObservedTrailingMultipleStatus = Literal[
    "resolved",
    "missing",
    "ambiguous",
    "invalid",
    "not_applicable",
]


class ThesisError(ValueError):
    """Raised when a thesis cannot be evaluated without inventing facts."""


def _date(value: object) -> date:
    if not isinstance(value, str):
        raise ValueError("must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("must be an ISO date string") from error


def _datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("must be an ISO datetime string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("must be an ISO datetime string") from error
    if parsed.tzinfo is None:
        raise ValueError("must include a timezone")
    return parsed


def _tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        raise ValueError("must be a decimal number")
    if isinstance(value, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) is None:
        raise ValueError("decimal string must use fixed-point notation")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("must be a decimal number") from error
    if not parsed.is_finite():
        raise ValueError("must be a finite decimal number")
    return parsed


class Source(BaseModel):
    model_config = _CONFIG

    source_id: Annotated[str, Field(min_length=1)]
    ticker: Annotated[str, Field(pattern=_TICKER)]
    source_tier: Literal["primary", "secondary", "local_data"]
    ref: Annotated[str, Field(min_length=1)] | None = None
    provider: Annotated[str, Field(min_length=1)] | None = None
    dataset: Annotated[str, Field(min_length=1)] | None = None
    retrieved_at: datetime
    as_of: date
    used_for: Annotated[str, Field(min_length=1)]

    @field_validator("as_of", mode="before")
    @classmethod
    def _parse_date(cls, value: object) -> date:
        return _date(value)

    @field_validator("retrieved_at", mode="before")
    @classmethod
    def _parse_time(cls, value: object) -> datetime:
        return _datetime(value)

    @model_validator(mode="after")
    def _source_locator(self) -> Source:
        if self.source_tier == "local_data":
            if self.ref is not None or self.provider is None or self.dataset is None:
                raise ValueError("local_data source requires provider and dataset and forbids ref")
        elif self.ref is None or not self.ref.startswith("https://"):
            raise ValueError("external source requires an HTTPS ref")
        elif self.provider is not None or self.dataset is not None:
            raise ValueError("external source forbids local provider and dataset")
        return self


class ObservedFact(BaseModel):
    model_config = _CONFIG

    fact_id: Annotated[str, Field(min_length=1)]
    fact_kind: Literal[
        "market_price",
        "valuation_metric",
        "net_income_attributable_to_owners",
        "fcfe",
        "shares_outstanding",
        "other",
    ]
    value: (
        str
        | bool
        | Annotated[int, Field(ge=-(10**30), le=10**30)]
        | Annotated[float, Field(ge=-1e30, le=1e30)]
    )
    unit: Annotated[str, Field(min_length=1)]
    as_of: date
    source_ids: tuple[Annotated[str, Field(min_length=1)], ...]
    observed_at: datetime | None = None
    price_basis: (
        Literal[
            "realtime",
            "last_close_adjusted",
            "last_close_unadjusted",
        ]
        | None
    ) = None

    @field_validator("as_of", mode="before")
    @classmethod
    def _parse_date(cls, value: object) -> date:
        return _date(value)

    @field_validator("source_ids", mode="before")
    @classmethod
    def _parse_sources(cls, value: object) -> object:
        return _tuple(value)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _parse_observed_at(cls, value: object) -> datetime | None:
        return None if value is None else _datetime(value)

    @model_validator(mode="after")
    def _market_price_metadata(self) -> ObservedFact:
        if self.fact_kind == "market_price":
            if self.observed_at is None or self.price_basis is None:
                raise ValueError("market_price requires observed_at and price_basis")
        elif self.observed_at is not None or self.price_basis is not None:
            raise ValueError("observed_at and price_basis are reserved for market_price")
        return self


class ScreeningEstimate(BaseModel):
    model_config = _CONFIG

    origin: Literal["estimate"]
    model_version: Annotated[str, Field(min_length=1)]
    as_of: date
    expected_return_annual_ratio: Annotated[float, Field(ge=-1, le=10)]
    expected_return_unit: Literal["annual_ratio"]
    fair_value_anchor_yen: (
        Annotated[
            Decimal,
            Field(gt=Decimal("0.0001"), le=Decimal("1000000000"), decimal_places=4),
        ]
        | None
    )
    fair_value_unit: Literal["JPY_per_share"]
    assumptions: Annotated[str, Field(min_length=1)]
    source_ids: Annotated[tuple[Annotated[str, Field(min_length=1)], ...], Field(min_length=1)]

    @field_validator("as_of", mode="before")
    @classmethod
    def _parse_date(cls, value: object) -> date:
        return _date(value)

    @field_validator("fair_value_anchor_yen", mode="before")
    @classmethod
    def _parse_fair_value(cls, value: object) -> Decimal | None:
        return None if value is None else _decimal(value)

    @field_validator("source_ids", mode="before")
    @classmethod
    def _parse_sources(cls, value: object) -> object:
        return _tuple(value)


class InputSnapshot(BaseModel):
    model_config = _CONFIG

    snapshot_version: Literal[1]
    producer_model_version: Annotated[str, Field(min_length=1)]
    ticker: Annotated[str, Field(pattern=_TICKER)]
    company_name: Annotated[str, Field(min_length=1)]
    sector: Annotated[str, Field(min_length=1)]
    common_factors: tuple[Annotated[str, Field(min_length=1)], ...]
    as_of: date
    sources: tuple[Source, ...]
    facts: tuple[ObservedFact, ...]
    screening_estimate: ScreeningEstimate | None = None

    @field_validator("as_of", mode="before")
    @classmethod
    def _parse_date(cls, value: object) -> date:
        return _date(value)

    @field_validator("sources", "facts", "common_factors", mode="before")
    @classmethod
    def _parse_sequences(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def _common_factor_shape(self) -> InputSnapshot:
        if len(set(self.common_factors)) != len(self.common_factors):
            raise ValueError("common_factors must not contain duplicates")
        if self.common_factors != tuple(sorted(self.common_factors)):
            raise ValueError("common_factors must be sorted")
        return self


class DerivedMetric(BaseModel):
    model_config = _CONFIG

    metric_id: Annotated[str, Field(min_length=1)]
    value: float
    unit: Annotated[str, Field(min_length=1)]
    as_of: date
    formula: Literal["ratio"]
    formula_version: Literal["ratio-v1"]
    input_fact_ids: tuple[Annotated[str, Field(min_length=1)], Annotated[str, Field(min_length=1)]]
    assumption: Annotated[str, Field(min_length=1)]
    source_ids: tuple[Annotated[str, Field(min_length=1)], ...]

    @field_validator("as_of", mode="before")
    @classmethod
    def _parse_date(cls, value: object) -> date:
        return _date(value)

    @field_validator("source_ids", "input_fact_ids", mode="before")
    @classmethod
    def _parse_sources(cls, value: object) -> object:
        return _tuple(value)


class DerivedNamespace(BaseModel):
    model_config = _CONFIG

    metrics: tuple[DerivedMetric, ...]

    @field_validator("metrics", mode="before")
    @classmethod
    def _parse_metrics(cls, value: object) -> object:
        return _tuple(value)


class ScenarioEstimate(BaseModel):
    model_config = _CONFIG

    horizon_years: Literal[3, 5]
    name: Literal["bear", "base", "bull"]
    earnings_basis: Literal["net_income_attributable_to_owners", "fcfe"]
    starting_earnings_fact_id: Annotated[str, Field(min_length=1)]
    starting_earnings_yen: Annotated[Decimal, Field(gt=0, le=Decimal("10000000000000000"))]
    annual_earnings_growth_pct: Annotated[float, Field(ge=-50, le=50)]
    starting_share_count_fact_id: Annotated[str, Field(min_length=1)]
    starting_share_count: Annotated[Decimal, Field(gt=0, le=Decimal("10000000000000"))]
    annual_share_count_change_pct: Annotated[float, Field(ge=-20, le=20)]
    terminal_valuation_multiple: Annotated[Decimal, Field(gt=0, le=100)]
    cumulative_dividend_per_share_yen: Annotated[Decimal, Field(ge=0, le=1000000000)]
    terminal_price_includes_dividends: Literal[False]
    claimed_terminal_earnings_yen: Annotated[Decimal, Field(gt=0, le=Decimal("1e18"))]
    claimed_terminal_share_count: Annotated[Decimal, Field(gt=0, le=Decimal("1e15"))]
    claimed_terminal_price_yen: Annotated[Decimal, Field(gt=0, le=Decimal("1e12"))]
    claimed_total_return_cagr_pct: Annotated[float, Field(ge=-100, le=1000)]
    as_of: date
    unit: Literal["JPY_per_share_total_return"]
    model_version: Annotated[str, Field(min_length=1)]
    assumption: Annotated[str, Field(min_length=1)]
    source_ids: tuple[Annotated[str, Field(min_length=1)], ...]

    @field_validator("as_of", mode="before")
    @classmethod
    def _parse_date(cls, value: object) -> date:
        return _date(value)

    @field_validator("source_ids", mode="before")
    @classmethod
    def _parse_sources(cls, value: object) -> object:
        return _tuple(value)

    @field_validator(
        "starting_earnings_yen",
        "starting_share_count",
        "terminal_valuation_multiple",
        "cumulative_dividend_per_share_yen",
        "claimed_terminal_earnings_yen",
        "claimed_terminal_share_count",
        "claimed_terminal_price_yen",
        mode="before",
    )
    @classmethod
    def _parse_decimals(cls, value: object) -> Decimal:
        return _decimal(value)


class ScreeningFVBridge(BaseModel):
    model_config = _CONFIG

    primary_driver: Literal[
        "earnings_normalization",
        "growth",
        "shares",
        "multiple",
        "dividend",
        "required_return",
        "other",
    ]
    note: Annotated[
        str,
        Field(
            min_length=1,
            pattern=(
                r"^(?:[^\r\n]*\S[^\r\n]*(?:\r?\n[^\r\n]*)?|"
                r"[^\r\n]*\r?\n[^\r\n]*\S[^\r\n]*)$"
            ),
        ),
    ]


class EstimatesNamespace(BaseModel):
    model_config = _CONFIG

    model_version: Annotated[str, Field(min_length=1)]
    market_price_fact_id: Annotated[str, Field(min_length=1)]
    entry_price_basis_yen: Annotated[
        Decimal, Field(ge=Decimal("0.0001"), le=Decimal("1000000000"), decimal_places=4)
    ]
    entry_price_basis: Literal["observed_market_price"]
    entry_price_source_ids: tuple[Annotated[str, Field(min_length=1)], ...]
    entry_price_assumption: Annotated[str, Field(min_length=1)]
    required_5y_base_cagr_pct: Annotated[float, Field(gt=0, le=100)]
    current_fair_value_yen: Annotated[
        Decimal, Field(gt=Decimal("0.0001"), le=Decimal("1000000000"), decimal_places=4)
    ]
    valuation_model_version: Annotated[str, Field(min_length=1)]
    fair_value_source_ids: Annotated[
        tuple[Annotated[str, Field(min_length=1)], ...], Field(min_length=1)
    ]
    scenarios: tuple[ScenarioEstimate, ...]
    screening_fv_bridge: ScreeningFVBridge | None = None

    @field_validator("scenarios", "entry_price_source_ids", "fair_value_source_ids", mode="before")
    @classmethod
    def _parse_scenarios(cls, value: object) -> object:
        return _tuple(value)

    @field_validator("entry_price_basis_yen", "current_fair_value_yen", mode="before")
    @classmethod
    def _parse_entry_price(cls, value: object) -> Decimal:
        return _decimal(value)


class PermanentLossRisk(BaseModel):
    model_config = _CONFIG

    axis: RiskAxis
    assessment: Literal["acceptable", "adverse", "unknown"]
    evidence_status: Literal["verified", "partially_verified", "unverified"]
    summary: Annotated[str, Field(min_length=1)]
    as_of: date
    source_ids: tuple[Annotated[str, Field(min_length=1)], ...]

    @field_validator("as_of", mode="before")
    @classmethod
    def _parse_date(cls, value: object) -> date:
        return _date(value)

    @field_validator("source_ids", mode="before")
    @classmethod
    def _parse_sources(cls, value: object) -> object:
        return _tuple(value)


class EvidenceOverride(BaseModel):
    model_config = _CONFIG

    override_id: Annotated[str, Field(min_length=1)]
    reason: Annotated[str, Field(min_length=1)]
    decision_reference: Annotated[str, Field(min_length=1)]
    proposal_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    review_id: Annotated[str, Field(min_length=1)]
    review_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    approved_by: Literal["human"]
    acknowledged_risk_axes: tuple[Annotated[str, Field(min_length=1)], ...]
    approved_at: datetime
    expires_at: datetime

    @field_validator("approved_at", "expires_at", mode="before")
    @classmethod
    def _parse_time(cls, value: object) -> datetime:
        return _datetime(value)

    @field_validator("acknowledged_risk_axes", mode="before")
    @classmethod
    def _parse_axes(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def _valid_window(self) -> EvidenceOverride:
        if self.expires_at <= self.approved_at:
            raise ValueError("evidence override expires_at must follow approved_at")
        if (self.expires_at - self.approved_at).total_seconds() > 31 * 86_400:
            raise ValueError("evidence override cannot exceed 31 days")
        return self


class AIValueCaptureJudgment(BaseModel):
    """Company-specific assessment of whether AI change reaches shareholders."""

    model_config = _CONFIG

    assessment_status: Literal["material", "not_material", "unknown"]
    roles: tuple[Literal["enabler", "infrastructure", "complement", "adopter", "disrupted"], ...]
    competitive_advantage: Literal["favorable", "neutral", "adverse", "unknown"]
    pricing_power: Literal["favorable", "neutral", "adverse", "unknown"]
    capex_burden: Literal["favorable", "neutral", "adverse", "unknown"]
    customer_bargaining_power: Literal["favorable", "neutral", "adverse", "unknown"]
    value_capture_conclusion: Literal["captured", "uncertain", "not_captured", "adverse"]
    decision_weight: Literal["none", "supporting", "material"]
    rationale: Annotated[str, Field(min_length=1)]
    source_ids: Annotated[tuple[Annotated[str, Field(min_length=1)], ...], Field(min_length=1)]

    @field_validator("roles", "source_ids", mode="before")
    @classmethod
    def _parse_sequences(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def _valid_decision_weight(self) -> AIValueCaptureJudgment:
        if len(set(self.roles)) != len(self.roles) or self.roles != tuple(sorted(self.roles)):
            raise ValueError("ai_value_capture.roles must be unique and sorted")
        if self.assessment_status == "material" and not self.roles:
            raise ValueError("material AI value-capture assessment requires at least one role")
        if self.assessment_status == "not_material" and (
            self.roles or self.decision_weight != "none"
        ):
            raise ValueError(
                "not_material AI value-capture assessment requires no roles and none weight"
            )
        if (
            self.assessment_status == "unknown"
            or self.value_capture_conclusion in {"uncertain", "not_captured", "adverse"}
        ) and self.decision_weight != "none":
            raise ValueError("uncertain or uncaptured AI value cannot carry decision weight")
        if self.decision_weight != "none" and (
            self.assessment_status != "material" or self.value_capture_conclusion != "captured"
        ):
            raise ValueError("AI decision weight requires material captured value")
        return self


class JudgmentNamespace(BaseModel):
    model_config = _CONFIG

    recommendation: Literal["buy", "defer", "reject"]
    proposed_at: datetime
    confidence: Literal["low", "medium", "high"]
    permanent_loss_conclusion: Literal["acceptable", "elevated", "unknown"]
    strongest_countercase: Annotated[str, Field(min_length=1)]
    sizing_action: Literal["normal", "reduced", "none"]
    ai_value_capture: AIValueCaptureJudgment

    @field_validator("proposed_at", mode="before")
    @classmethod
    def _parse_time(cls, value: object) -> datetime:
        return _datetime(value)


class ReviewedScenario(BaseModel):
    model_config = _CONFIG

    horizon_years: Literal[3, 5]
    name: Literal["bear", "base", "bull"]
    total_return_cagr_pct: Annotated[float, Field(ge=-100, le=1000)]


class IndependentReview(BaseModel):
    model_config = _CONFIG

    review_id: Annotated[str, Field(min_length=1)]
    reviewer_role: Literal["independent_second_pass"]
    reviewer_identity: Annotated[str, Field(min_length=1)]
    reviewer_run_id: Annotated[str, Field(min_length=1)]
    reviewed_at: datetime
    reviewed_thesis_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    primary_source_check: Literal["verified", "partially_verified", "unverified"]
    checked_source_ids: tuple[Annotated[str, Field(min_length=1)], ...]
    recalculated_scenarios: tuple[ReviewedScenario, ...]
    strongest_countercase: Annotated[str, Field(min_length=1)]
    alternative_candidate_check: Literal["compared", "unavailable"]
    proposal_changed: bool
    change_rationale: str | None = None

    @field_validator("reviewed_at", mode="before")
    @classmethod
    def _parse_time(cls, value: object) -> datetime:
        return _datetime(value)

    @field_validator("checked_source_ids", "recalculated_scenarios", mode="before")
    @classmethod
    def _parse_sequences(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def _change_has_reason(self) -> IndependentReview:
        if self.proposal_changed != bool(self.change_rationale and self.change_rationale.strip()):
            raise ValueError("proposal_changed and change_rationale must be specified together")
        return self


class ThesisDocument(BaseModel):
    """Strict persisted contract with no legacy thesis compatibility fields."""

    model_config = _CONFIG

    schema_version: Literal[2]
    input_snapshot: InputSnapshot
    derived: DerivedNamespace
    estimates: EstimatesNamespace
    permanent_loss_risks: tuple[PermanentLossRisk, ...]
    judgment: JudgmentNamespace
    independent_review_ref: Annotated[str, Field(min_length=1)] | None = None
    human_evidence_override: EvidenceOverride | None = None

    @field_validator("permanent_loss_risks", mode="before")
    @classmethod
    def _parse_risks(cls, value: object) -> object:
        return _tuple(value)


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    horizon_years: int
    name: str
    terminal_earnings_yen: float
    terminal_share_count: float
    terminal_price_yen: float
    total_return_cagr_pct: float


@dataclass(frozen=True, slots=True)
class FiveYearBaseBreakEvenResult:
    required_total_value_yen: Decimal | None
    required_total_return_cagr_pct: Decimal
    base_terminal_valuation_multiple: Decimal
    break_even_terminal_valuation_multiple: Decimal | None
    terminal_multiple_downside_buffer: Decimal | None
    terminal_multiple_status: TerminalMultipleStatus
    base_annual_earnings_growth_pct: Decimal
    break_even_annual_earnings_growth_pct: Decimal | None
    earnings_growth_downside_buffer_pct_points: Decimal | None
    earnings_growth_status: EarningsGrowthStatus
    observed_trailing_multiple_status: ObservedTrailingMultipleStatus
    observed_trailing_multiple_fact_id: str | None
    observed_trailing_multiple: Decimal | None
    base_terminal_multiple_minus_observed: Decimal | None
    base_terminal_multiple_premium_pct: Decimal | None


@dataclass(frozen=True, slots=True)
class ThesisResult:
    thesis_status: Literal["incomplete", "review_required", "ready", "ready_with_warnings"]
    decision_readiness: Literal["not_ready", "ready"]
    thesis_sha256: str
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    scenarios: tuple[ScenarioResult, ...]
    five_year_base_break_even: FiveYearBaseBreakEvenResult | None = None
    screening_fv_revision_pct: Decimal | None = None


@dataclass(frozen=True, slots=True)
class _CurrentThesisEligibility:
    status: Literal["current_ready", "expired_override_only", "invalid"]
    result: ThesisResult


def load_thesis(path: Path) -> ThesisDocument:
    """Load a strict YAML thesis."""

    try:
        raw = safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ThesisError(f"failed to load thesis: {error}") from error
    if not isinstance(raw, Mapping):
        raise ThesisError("thesis root must be a mapping")
    try:
        return ThesisDocument.model_validate(raw)
    except ValidationError as error:
        raise ThesisError(str(error)) from error


def load_independent_review(path: Path) -> IndependentReview:
    """Load a second-pass artifact independently from its reviewed thesis."""

    try:
        raw = safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ThesisError(f"failed to load independent review: {error}") from error
    if not isinstance(raw, Mapping):
        raise ThesisError("independent review root must be a mapping")
    try:
        return IndependentReview.model_validate(raw)
    except ValidationError as error:
        raise ThesisError(str(error)) from error


def evaluate_thesis(
    document: ThesisDocument,
    *,
    review: IndependentReview | None = None,
    now: datetime | None = None,
) -> ThesisResult:
    """Recalculate scenarios and determine whether the proposal is decision-ready."""

    errors: list[str] = []
    warnings: list[str] = []
    evaluated_at = _evaluation_instant(now)
    if document.input_snapshot.as_of > evaluated_at.date():
        errors.append("thesis as_of cannot be in the future")
    if document.judgment.proposed_at.date() < document.input_snapshot.as_of:
        errors.append("proposal cannot predate thesis as_of")
    if document.judgment.proposed_at > evaluated_at:
        errors.append("proposal cannot be future-dated")
    source_ids = {source.source_id for source in document.input_snapshot.sources}
    source_tiers = {
        source.source_id: source.source_tier for source in document.input_snapshot.sources
    }
    if len(source_ids) != len(document.input_snapshot.sources):
        errors.append("source_id must be unique")
    if not source_ids:
        errors.append("input_snapshot.sources must not be empty")
    for source in document.input_snapshot.sources:
        if source.ticker != document.input_snapshot.ticker:
            errors.append(f"source {source.source_id} ticker does not match input_snapshot")
        if source.as_of > document.input_snapshot.as_of:
            errors.append(f"source {source.source_id} is after thesis as_of")
        if source.retrieved_at.date() < source.as_of:
            errors.append(f"source {source.source_id} was retrieved before its as_of")
        if source.retrieved_at > evaluated_at:
            errors.append(f"source {source.source_id} retrieval is future-dated")
        if source.retrieved_at > document.judgment.proposed_at:
            errors.append(f"source {source.source_id} was retrieved after the AI proposal")
    _check_lineage(document, source_ids, errors)
    _check_snapshot_contract(document, errors)
    _check_screening_fv_bridge(document, source_tiers, errors, warnings)
    required_review_source_ids = set(document.estimates.entry_price_source_ids)
    required_review_source_ids.update(document.estimates.fair_value_source_ids)
    for fact in document.input_snapshot.facts:
        required_review_source_ids.update(fact.source_ids)
    for metric in document.derived.metrics:
        required_review_source_ids.update(metric.source_ids)
    for scenario in document.estimates.scenarios:
        required_review_source_ids.update(scenario.source_ids)
    for risk in document.permanent_loss_risks:
        required_review_source_ids.update(risk.source_ids)
    required_review_source_ids.update(document.judgment.ai_value_capture.source_ids)

    try:
        scenarios = tuple(
            _recalculate_scenario(item, entry_price=document.estimates.entry_price_basis_yen)
            for item in document.estimates.scenarios
        )
    except (ArithmeticError, OverflowError, ValueError) as error:
        raise ThesisError(f"scenario calculation failed: {error}") from error
    _check_derived_metrics(document, errors)
    _check_scenario_fact_inputs(document, errors)
    keys = {(item.horizon_years, item.name) for item in document.estimates.scenarios}
    if keys != _SCENARIO_KEYS or len(keys) != len(document.estimates.scenarios):
        errors.append("scenarios must contain each bear/base/bull 3y/5y pair exactly once")
    by_horizon: dict[int, dict[str, float]] = {3: {}, 5: {}}
    for supplied, calculated in zip(document.estimates.scenarios, scenarios, strict=True):
        by_horizon[supplied.horizon_years][supplied.name] = calculated.total_return_cagr_pct
        _compare_claims(supplied, calculated, errors)
        if supplied.model_version != document.estimates.model_version:
            errors.append(
                f"scenario {supplied.horizon_years}y/{supplied.name} model version mismatch"
            )
        if supplied.as_of != document.input_snapshot.as_of:
            errors.append(f"scenario {supplied.horizon_years}y/{supplied.name} as_of mismatch")
    for horizon, values in by_horizon.items():
        if set(values) == {"bear", "base", "bull"} and not (
            values["bear"] <= values["base"] <= values["bull"]
        ):
            errors.append(f"scenario total returns are not ordered for {horizon}y")

    risk_by_axis = {risk.axis: risk for risk in document.permanent_loss_risks}
    if len(risk_by_axis) != len(document.permanent_loss_risks):
        errors.append("permanent-loss risk axes must be unique")
    for axis in sorted(_RISK_AXES - set(risk_by_axis)):
        errors.append(f"missing permanent-loss risk axis: {axis}")
    expected_conclusion = _risk_conclusion(document.permanent_loss_risks)
    if document.judgment.permanent_loss_conclusion != expected_conclusion:
        errors.append("judgment.permanent_loss_conclusion contradicts permanent-loss risk axes")
    _check_ai_value_capture(document, source_ids, risk_by_axis, errors)
    evidence_gaps = [
        risk.axis
        for risk in document.permanent_loss_risks
        if risk.evidence_status != "verified"
        or risk.assessment == "unknown"
        or not any(source_tiers.get(source_id) == "primary" for source_id in risk.source_ids)
    ]
    if evidence_gaps:
        warnings.append(f"permanent-loss evidence incomplete: {sorted(evidence_gaps)}")
    if evidence_gaps and document.judgment.confidence == "high":
        errors.append("high confidence is not allowed with incomplete primary evidence")

    adverse_axes = [
        risk.axis for risk in document.permanent_loss_risks if risk.assessment == "adverse"
    ]
    if adverse_axes:
        warnings.append(f"permanent-loss risk is adverse: {sorted(adverse_axes)}")
    exception_axes = sorted(set(evidence_gaps + adverse_axes))
    override = document.human_evidence_override
    if override is not None and not set(exception_axes).issubset(override.acknowledged_risk_axes):
        errors.append("evidence override must acknowledge every incomplete or adverse risk axis")

    core_hash = thesis_core_hash(document)
    if document.judgment.recommendation == "buy":
        if review is None or document.independent_review_ref is None:
            errors.append("buy recommendation requires an independent second-pass review")
        else:
            _check_review(
                review,
                core_hash,
                document.input_snapshot,
                document.judgment,
                evaluated_at,
                source_ids,
                required_review_source_ids,
                scenarios,
                errors,
                warnings,
            )
            valid_evidence_override = _has_valid_evidence_override(
                document, review=review, evaluated_at=evaluated_at
            )
            if exception_axes and not valid_evidence_override:
                errors.append(_INCOMPLETE_EVIDENCE_OVERRIDE_REQUIRED)
            if review.primary_source_check != "verified" and not valid_evidence_override:
                errors.append(_PRIMARY_REVIEW_OVERRIDE_REQUIRED)
    elif review is not None:
        _check_review(
            review,
            core_hash,
            document.input_snapshot,
            document.judgment,
            evaluated_at,
            source_ids,
            required_review_source_ids,
            scenarios,
            errors,
            warnings,
        )

    if errors:
        status: Literal["incomplete", "review_required", "ready", "ready_with_warnings"] = (
            "review_required"
            if errors == ["buy recommendation requires an independent second-pass review"]
            else "incomplete"
        )
    else:
        status = "ready_with_warnings" if warnings else "ready"
    five_year_base = next(
        (
            item
            for item in document.estimates.scenarios
            if item.horizon_years == 5 and item.name == "base"
        ),
        None,
    )
    return ThesisResult(
        thesis_status=status,
        decision_readiness="ready" if status in {"ready", "ready_with_warnings"} else "not_ready",
        thesis_sha256=core_hash,
        errors=tuple(errors),
        warnings=tuple(warnings),
        scenarios=tuple(
            sorted(
                scenarios,
                key=lambda item: (item.horizon_years, _SCENARIO_ORDER[item.name]),
            )
        ),
        five_year_base_break_even=(
            _calculate_five_year_base_break_even(document, five_year_base)
            if five_year_base is not None
            else None
        ),
        screening_fv_revision_pct=_calculate_screening_fv_revision_pct(document),
    )


def _classify_current_thesis_eligibility(
    document: ThesisDocument,
    *,
    review: IndependentReview,
    now: datetime,
) -> _CurrentThesisEligibility:
    """Classify current readiness without exposing override policy to consumers."""
    evaluated_at = _evaluation_instant(now)
    result = evaluate_thesis(document, review=review, now=evaluated_at)
    if not result.errors and result.decision_readiness == "ready":
        return _CurrentThesisEligibility("current_ready", result)
    override_status = _evidence_override_status(
        document,
        review=review,
        evaluated_at=evaluated_at,
    )
    if (
        override_status == "expired"
        and result.errors
        and set(result.errors).issubset(_EXPIRY_ONLY_ERRORS)
    ):
        return _CurrentThesisEligibility("expired_override_only", result)
    return _CurrentThesisEligibility("invalid", result)


def _evaluation_instant(now: datetime | None) -> datetime:
    evaluated_at = now or datetime.now(tz=ZoneInfo("Asia/Tokyo"))
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise ThesisError("evaluation instant must include a timezone")
    return evaluated_at.astimezone(ZoneInfo("Asia/Tokyo"))


def thesis_core_hash(document: ThesisDocument) -> str:
    payload = document.model_dump(mode="json", exclude={"human_evidence_override"})
    if document.input_snapshot.screening_estimate is None:
        input_snapshot = payload.get("input_snapshot")
        if isinstance(input_snapshot, dict):
            input_snapshot.pop("screening_estimate", None)
    if document.estimates.screening_fv_bridge is None:
        estimates = payload.get("estimates")
        if isinstance(estimates, dict):
            estimates.pop("screening_fv_bridge", None)
    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (OverflowError, ValueError) as error:
        raise ThesisError(f"thesis cannot be hashed: {error}") from error
    return hashlib.sha256(encoded.encode()).hexdigest()


def independent_review_hash(review: IndependentReview) -> str:
    payload = review.model_dump(mode="json")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def result_to_payload(result: ThesisResult) -> dict[str, object]:
    return {
        "thesis_status": result.thesis_status,
        "decision_readiness": result.decision_readiness,
        "thesis_sha256": result.thesis_sha256,
        "errors": list(result.errors),
        "warnings": list(result.warnings),
        "scenarios": [
            {
                "horizon_years": item.horizon_years,
                "name": item.name,
                "terminal_earnings_yen": item.terminal_earnings_yen,
                "terminal_share_count": item.terminal_share_count,
                "terminal_price_yen": item.terminal_price_yen,
                "total_return_cagr_pct": item.total_return_cagr_pct,
            }
            for item in result.scenarios
        ],
        "five_year_base_break_even": _five_year_base_break_even_to_payload(
            result.five_year_base_break_even
        ),
        "screening_fv_revision_pct": _round_payload_decimal(result.screening_fv_revision_pct),
    }


def _calculate_screening_fv_revision_pct(
    document: ThesisDocument,
) -> Decimal | None:
    screening_estimate = document.input_snapshot.screening_estimate
    if screening_estimate is None or screening_estimate.fair_value_anchor_yen is None:
        return None
    try:
        with localcontext() as context:
            context.prec = 50
            revision_pct = (
                document.estimates.current_fair_value_yen / screening_estimate.fair_value_anchor_yen
                - Decimal(1)
            ) * Decimal(100)
    except (DecimalException, ArithmeticError, OverflowError, ValueError):
        return None
    return revision_pct if revision_pct.is_finite() else None


def _calculate_five_year_base_break_even(
    document: ThesisDocument,
    scenario: ScenarioEstimate,
) -> FiveYearBaseBreakEvenResult:
    required_return = Decimal(str(document.estimates.required_5y_base_cagr_pct))
    base_multiple = scenario.terminal_valuation_multiple
    base_growth = Decimal(str(scenario.annual_earnings_growth_pct))
    (
        observed_status,
        observed_fact_id,
        observed_multiple,
        base_minus_observed,
        base_premium_pct,
    ) = _observed_trailing_multiple(document, scenario)

    required_total_value: Decimal | None = None
    break_even_multiple: Decimal | None = None
    multiple_buffer: Decimal | None = None
    multiple_status: TerminalMultipleStatus = "calculation_unresolved"
    break_even_growth: Decimal | None = None
    growth_buffer: Decimal | None = None
    growth_status: EarningsGrowthStatus = "calculation_unresolved"
    try:
        with localcontext() as context:
            context.prec = 50
            one = Decimal(1)
            hundred = Decimal(100)
            horizon = Decimal(scenario.horizon_years)
            required_total_value = (
                document.estimates.entry_price_basis_yen
                * (one + required_return / hundred) ** scenario.horizon_years
            )
            if not required_total_value.is_finite():
                raise ArithmeticError("required total value must be finite")

            dividends = scenario.cumulative_dividend_per_share_yen
            if dividends >= required_total_value:
                multiple_status = "dividends_alone_sufficient"
                growth_status = "dividends_alone_sufficient"
            else:
                terminal_shares = (
                    scenario.starting_share_count
                    * (one + Decimal(str(scenario.annual_share_count_change_pct)) / hundred)
                    ** scenario.horizon_years
                )
                terminal_earnings = (
                    scenario.starting_earnings_yen
                    * (one + base_growth / hundred) ** scenario.horizon_years
                )
                break_even_multiple = (
                    (required_total_value - dividends) * terminal_shares / terminal_earnings
                )
                if not break_even_multiple.is_finite():
                    raise ArithmeticError("break-even terminal multiple must be finite")
                multiple_buffer = base_multiple - break_even_multiple
                multiple_status = _terminal_multiple_status(break_even_multiple)

                earnings_growth_ratio = (
                    (required_total_value - dividends)
                    * terminal_shares
                    / (scenario.starting_earnings_yen * base_multiple)
                )
                if not earnings_growth_ratio.is_finite() or earnings_growth_ratio <= 0:
                    raise ArithmeticError("break-even earnings growth ratio must be positive")
                break_even_growth = ((earnings_growth_ratio.ln() / horizon).exp() - one) * hundred
                if not break_even_growth.is_finite():
                    raise ArithmeticError("break-even earnings growth must be finite")
                growth_buffer = base_growth - break_even_growth
                growth_status = _earnings_growth_status(break_even_growth)
    except (DecimalException, ArithmeticError, OverflowError, ValueError):
        required_total_value = None
        break_even_multiple = None
        multiple_buffer = None
        multiple_status = "calculation_unresolved"
        break_even_growth = None
        growth_buffer = None
        growth_status = "calculation_unresolved"

    return FiveYearBaseBreakEvenResult(
        required_total_value_yen=required_total_value,
        required_total_return_cagr_pct=required_return,
        base_terminal_valuation_multiple=base_multiple,
        break_even_terminal_valuation_multiple=break_even_multiple,
        terminal_multiple_downside_buffer=multiple_buffer,
        terminal_multiple_status=multiple_status,
        base_annual_earnings_growth_pct=base_growth,
        break_even_annual_earnings_growth_pct=break_even_growth,
        earnings_growth_downside_buffer_pct_points=growth_buffer,
        earnings_growth_status=growth_status,
        observed_trailing_multiple_status=observed_status,
        observed_trailing_multiple_fact_id=observed_fact_id,
        observed_trailing_multiple=observed_multiple,
        base_terminal_multiple_minus_observed=base_minus_observed,
        base_terminal_multiple_premium_pct=base_premium_pct,
    )


def _terminal_multiple_status(value: Decimal) -> TerminalMultipleStatus:
    if value <= 0:
        return "below_model_min"
    if value > 100:
        return "above_model_max"
    return "within_model_bounds"


def _earnings_growth_status(value: Decimal) -> EarningsGrowthStatus:
    if value < -50:
        return "below_model_min"
    if value > 50:
        return "above_model_max"
    return "within_model_bounds"


def _observed_trailing_multiple(
    document: ThesisDocument,
    scenario: ScenarioEstimate,
) -> tuple[
    ObservedTrailingMultipleStatus,
    str | None,
    Decimal | None,
    Decimal | None,
    Decimal | None,
]:
    if scenario.earnings_basis != "net_income_attributable_to_owners":
        return "not_applicable", None, None, None, None

    candidates = [
        fact
        for fact in document.input_snapshot.facts
        if fact.fact_id == "trailing-per" or fact.fact_id.startswith("trailing-per-")
    ]
    if not candidates:
        return "missing", None, None, None, None
    if len(candidates) > 1:
        return "ambiguous", None, None, None, None

    fact = candidates[0]
    try:
        value = _observed_fact_decimal(fact.value)
    except (InvalidOperation, TypeError, ValueError):
        value = None
    resolved_sources: list[Source] = []
    for source_id in fact.source_ids:
        matches = [
            source for source in document.input_snapshot.sources if source.source_id == source_id
        ]
        if len(matches) != 1:
            return "invalid", fact.fact_id, None, None, None
        resolved_sources.append(matches[0])
    if (
        fact.fact_kind != "valuation_metric"
        or fact.unit != "ratio"
        or fact.as_of != document.input_snapshot.as_of
        or value is None
        or value <= 0
        or not any(source.source_tier == "local_data" for source in resolved_sources)
    ):
        return "invalid", fact.fact_id, None, None, None

    base_multiple = scenario.terminal_valuation_multiple
    return (
        "resolved",
        fact.fact_id,
        value,
        base_multiple - value,
        (base_multiple / value - Decimal(1)) * Decimal(100),
    )


def _observed_fact_decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise TypeError("observed multiple must be numeric")
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError("observed multiple must be finite")
    return parsed


def _five_year_base_break_even_to_payload(
    result: FiveYearBaseBreakEvenResult | None,
) -> dict[str, object] | None:
    if result is None:
        return None
    return {
        "required_total_value_yen": _round_payload_decimal(result.required_total_value_yen),
        "required_total_return_cagr_pct": _round_payload_decimal(
            result.required_total_return_cagr_pct
        ),
        "base_terminal_valuation_multiple": _round_payload_decimal(
            result.base_terminal_valuation_multiple
        ),
        "break_even_terminal_valuation_multiple": _round_payload_decimal(
            result.break_even_terminal_valuation_multiple
        ),
        "terminal_multiple_downside_buffer": _round_payload_decimal(
            result.terminal_multiple_downside_buffer
        ),
        "terminal_multiple_status": result.terminal_multiple_status,
        "base_annual_earnings_growth_pct": _round_payload_decimal(
            result.base_annual_earnings_growth_pct
        ),
        "break_even_annual_earnings_growth_pct": _round_payload_decimal(
            result.break_even_annual_earnings_growth_pct
        ),
        "earnings_growth_downside_buffer_pct_points": _round_payload_decimal(
            result.earnings_growth_downside_buffer_pct_points
        ),
        "earnings_growth_status": result.earnings_growth_status,
        "observed_trailing_multiple_status": result.observed_trailing_multiple_status,
        "observed_trailing_multiple_fact_id": result.observed_trailing_multiple_fact_id,
        "observed_trailing_multiple": _round_payload_decimal(result.observed_trailing_multiple),
        "base_terminal_multiple_minus_observed": _round_payload_decimal(
            result.base_terminal_multiple_minus_observed
        ),
        "base_terminal_multiple_premium_pct": _round_payload_decimal(
            result.base_terminal_multiple_premium_pct
        ),
    }


def _round_payload_decimal(value: Decimal | None) -> float | None:
    if value is None:
        return None
    try:
        with localcontext() as context:
            integer_digits = max(value.adjusted() + 1, 1)
            context.prec = max(50, integer_digits + 5)
            rounded = value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            payload_value = float(rounded)
    except (DecimalException, OverflowError, ValueError):
        return None
    return payload_value if math.isfinite(payload_value) else None


def _recalculate_scenario(scenario: ScenarioEstimate, *, entry_price: Decimal) -> ScenarioResult:
    horizon = scenario.horizon_years
    terminal_earnings = (
        float(scenario.starting_earnings_yen)
        * (1 + scenario.annual_earnings_growth_pct / 100) ** horizon
    )
    terminal_shares = (
        float(scenario.starting_share_count)
        * (1 + scenario.annual_share_count_change_pct / 100) ** horizon
    )
    terminal_price = (
        terminal_earnings / terminal_shares * float(scenario.terminal_valuation_multiple)
    )
    total_value = terminal_price + float(scenario.cumulative_dividend_per_share_yen)
    cagr = ((total_value / float(entry_price)) ** (1 / horizon) - 1) * 100
    if not all(
        math.isfinite(value) for value in (terminal_earnings, terminal_shares, terminal_price, cagr)
    ):
        raise ThesisError(f"scenario {horizon}y/{scenario.name} calculation must remain finite")
    return ScenarioResult(
        horizon_years=horizon,
        name=scenario.name,
        terminal_earnings_yen=round(terminal_earnings, 2),
        terminal_share_count=round(terminal_shares, 4),
        terminal_price_yen=round(terminal_price, 4),
        total_return_cagr_pct=round(cagr, 2),
    )


def _compare_claims(
    supplied: ScenarioEstimate, calculated: ScenarioResult, errors: list[str]
) -> None:
    key = f"{supplied.horizon_years}y/{supplied.name}"
    checks = (
        (
            "terminal earnings",
            float(supplied.claimed_terminal_earnings_yen),
            calculated.terminal_earnings_yen,
            0.01,
        ),
        (
            "terminal share count",
            float(supplied.claimed_terminal_share_count),
            calculated.terminal_share_count,
            0.0001,
        ),
        (
            "terminal price",
            float(supplied.claimed_terminal_price_yen),
            calculated.terminal_price_yen,
            0.0001,
        ),
        (
            "total-return CAGR",
            supplied.claimed_total_return_cagr_pct,
            calculated.total_return_cagr_pct,
            0.01,
        ),
    )
    for label, claimed, expected, tolerance in checks:
        if not math.isclose(claimed, expected, abs_tol=tolerance):
            errors.append(f"scenario {key} {label} mismatch: expected {expected}, got {claimed}")


def _check_derived_metrics(document: ThesisDocument, errors: list[str]) -> None:
    facts = {fact.fact_id: fact for fact in document.input_snapshot.facts}
    metric_ids = {metric.metric_id for metric in document.derived.metrics}
    if len(metric_ids) != len(document.derived.metrics):
        errors.append("derived metric_id must be unique")
    for metric in document.derived.metrics:
        inputs = [facts.get(fact_id) for fact_id in metric.input_fact_ids]
        if any(item is None for item in inputs):
            errors.append(f"metric {metric.metric_id} references unknown input fact")
            continue
        values: list[Decimal] = []
        units: list[str] = []
        for item in inputs:
            assert item is not None
            if isinstance(item.value, bool | str):
                errors.append(f"metric {metric.metric_id} input facts must be numeric")
                break
            value = Decimal(str(item.value))
            if abs(value) > Decimal("1e30"):
                errors.append(f"metric {metric.metric_id} input fact magnitude is too large")
                break
            values.append(value)
            units.append(item.unit)
        if len(values) != 2:
            continue
        if values[1] == 0:
            errors.append(f"metric {metric.metric_id} ratio denominator cannot be zero")
            continue
        expected = values[0] / values[1]
        if abs(expected) > Decimal("1e300"):
            errors.append(f"metric {metric.metric_id} ratio result magnitude is too large")
            continue
        expected_unit = {
            ("JPY", "shares"): "JPY_per_share",
            ("JPY", "JPY"): "ratio",
            ("shares", "shares"): "ratio",
        }.get((units[0], units[1]))
        if expected_unit is None or metric.unit != expected_unit:
            errors.append(
                f"metric {metric.metric_id} unit mismatch for ratio inputs {tuple(units)}"
            )
        if not math.isclose(metric.value, float(expected), rel_tol=1e-12, abs_tol=1e-12):
            errors.append(
                f"metric {metric.metric_id} value mismatch: expected {expected}, got {metric.value}"
            )


def _check_snapshot_contract(document: ThesisDocument, errors: list[str]) -> None:
    """Enforce the minimum self-contained decision-time input snapshot."""

    facts = document.input_snapshot.facts
    market_prices = [fact for fact in facts if fact.fact_kind == "market_price"]
    valuations = [fact for fact in facts if fact.fact_kind == "valuation_metric"]
    if len(market_prices) != 1:
        errors.append("input_snapshot requires exactly one market_price fact")
    if not valuations:
        errors.append("input_snapshot requires at least one valuation_metric fact")
    for fact in market_prices:
        if fact.fact_id != document.estimates.market_price_fact_id:
            errors.append("estimates.market_price_fact_id does not match snapshot market price")
        if fact.unit != "JPY_per_share":
            errors.append("market_price fact unit must be JPY_per_share")
        if fact.as_of != document.input_snapshot.as_of:
            errors.append("market_price fact as_of must equal input_snapshot as_of")
        if fact.observed_at is not None:
            if fact.observed_at.date() != fact.as_of:
                errors.append("market_price observed_at date must equal its as_of")
            if fact.observed_at > document.judgment.proposed_at:
                errors.append("market_price was observed after the AI proposal")
        if isinstance(fact.value, bool | str) or fact.value <= 0:
            errors.append("market_price fact must be a positive number")
        elif Decimal(str(fact.value)) != document.estimates.entry_price_basis_yen:
            errors.append("observed_market_price entry basis must equal snapshot market price")
    allowed_valuation_units = {"ratio", "percent", "JPY_per_share"}
    for fact in valuations:
        if fact.unit not in allowed_valuation_units:
            errors.append(f"valuation fact {fact.fact_id} has unsupported unit {fact.unit}")
        if isinstance(fact.value, bool | str):
            errors.append(f"valuation fact {fact.fact_id} must be numeric")


def _check_screening_fv_bridge(
    document: ThesisDocument,
    source_tiers: Mapping[str, str],
    errors: list[str],
    warnings: list[str],
) -> None:
    screening_estimate = document.input_snapshot.screening_estimate
    bridge = document.estimates.screening_fv_bridge
    if screening_estimate is not None:
        if screening_estimate.as_of != document.input_snapshot.as_of:
            errors.append("input_snapshot.screening_estimate.as_of must equal thesis as_of")
        if not any(
            source_tiers.get(source_id) == "local_data"
            for source_id in screening_estimate.source_ids
        ):
            errors.append("input_snapshot.screening_estimate requires a local_data source")
    anchor = None if screening_estimate is None else screening_estimate.fair_value_anchor_yen
    if bridge is not None and anchor is None:
        errors.append(
            "estimates.screening_fv_bridge requires "
            "input_snapshot.screening_estimate.fair_value_anchor_yen"
        )
    elif anchor is not None and bridge is None:
        warnings.append("screening fair-value anchor has no screening_fv_bridge")


def _check_scenario_fact_inputs(document: ThesisDocument, errors: list[str]) -> None:
    facts = {fact.fact_id: fact for fact in document.input_snapshot.facts}
    if len(facts) != len(document.input_snapshot.facts):
        errors.append("input_snapshot fact_id must be unique")
    for scenario in document.estimates.scenarios:
        key = f"{scenario.horizon_years}y/{scenario.name}"
        pairs = (
            (
                "starting earnings",
                scenario.starting_earnings_fact_id,
                scenario.starting_earnings_yen,
                "JPY",
                scenario.earnings_basis,
            ),
            (
                "starting share count",
                scenario.starting_share_count_fact_id,
                scenario.starting_share_count,
                "shares",
                "shares_outstanding",
            ),
        )
        for label, fact_id, supplied, expected_unit, expected_kind in pairs:
            fact = facts.get(fact_id)
            if fact is None:
                errors.append(f"scenario {key} {label} references unknown fact {fact_id}")
                continue
            if fact.unit != expected_unit:
                errors.append(
                    f"scenario {key} {label} fact unit must be {expected_unit}, got {fact.unit}"
                )
            if fact.fact_kind != expected_kind:
                errors.append(
                    f"scenario {key} {label} fact kind must be {expected_kind}, "
                    f"got {fact.fact_kind}"
                )
            if isinstance(fact.value, bool | str):
                errors.append(f"scenario {key} {label} fact must be numeric")
                continue
            if Decimal(str(fact.value)) != supplied:
                errors.append(f"scenario {key} {label} does not match observed fact {fact_id}")


def _check_lineage(document: ThesisDocument, source_ids: set[str], errors: list[str]) -> None:
    rows: list[tuple[str, date, tuple[str, ...]]] = []
    rows.extend(
        (f"fact {item.fact_id}", item.as_of, item.source_ids)
        for item in document.input_snapshot.facts
    )
    rows.extend(
        (f"metric {item.metric_id}", item.as_of, item.source_ids)
        for item in document.derived.metrics
    )
    rows.extend(
        (f"scenario {item.horizon_years}y/{item.name}", item.as_of, item.source_ids)
        for item in document.estimates.scenarios
    )
    rows.append(
        (
            "estimate entry price basis",
            document.input_snapshot.as_of,
            document.estimates.entry_price_source_ids,
        )
    )
    rows.append(
        (
            "estimate current fair value",
            document.input_snapshot.as_of,
            document.estimates.fair_value_source_ids,
        )
    )
    if document.input_snapshot.screening_estimate is not None:
        rows.append(
            (
                "screening estimate",
                document.input_snapshot.screening_estimate.as_of,
                document.input_snapshot.screening_estimate.source_ids,
            )
        )
    rows.extend(
        (f"risk {item.axis}", item.as_of, item.source_ids) for item in document.permanent_loss_risks
    )
    rows.append(
        (
            "AI value capture",
            document.input_snapshot.as_of,
            document.judgment.ai_value_capture.source_ids,
        )
    )
    sources = {source.source_id: source for source in document.input_snapshot.sources}
    for label, as_of, references in rows:
        if not references:
            errors.append(f"{label} requires source_ids")
        unknown = sorted(set(references) - source_ids)
        if unknown:
            errors.append(f"{label} references unknown sources: {unknown}")
        if as_of > document.input_snapshot.as_of:
            errors.append(f"{label} as_of is after thesis as_of")
        if (document.input_snapshot.as_of - as_of).days > 400:
            errors.append(f"{label} is more than 400 days older than thesis as_of")
        for source_id in references:
            source = sources.get(source_id)
            if source is not None and (as_of - source.as_of).days > 400:
                errors.append(f"{label} source {source_id} is more than 400 days old")


def _check_ai_value_capture(
    document: ThesisDocument,
    source_ids: set[str],
    risk_by_axis: Mapping[RiskAxis, PermanentLossRisk],
    errors: list[str],
) -> None:
    assessment = document.judgment.ai_value_capture
    unknown = sorted(set(assessment.source_ids) - source_ids)
    if unknown:
        errors.append(f"AI value capture references unknown sources: {unknown}")
    if not assessment.source_ids:
        errors.append("AI value capture requires source_ids")
    if "disrupted" not in assessment.roles:
        return
    structural_decline = risk_by_axis.get("structural_decline")
    if structural_decline is None:
        return
    if structural_decline.assessment not in {"adverse", "unknown"}:
        errors.append("disrupted AI role requires adverse or unknown structural_decline risk")
    if not set(assessment.source_ids) & set(structural_decline.source_ids):
        errors.append("disrupted AI role requires a shared structural_decline source")


def _risk_conclusion(risks: tuple[PermanentLossRisk, ...]) -> str:
    if any(risk.assessment == "adverse" for risk in risks):
        return "elevated"
    if any(risk.assessment == "unknown" for risk in risks):
        return "unknown"
    return "acceptable"


def _has_valid_evidence_override(
    document: ThesisDocument,
    *,
    review: IndependentReview,
    evaluated_at: datetime,
) -> bool:
    return (
        _evidence_override_status(
            document,
            review=review,
            evaluated_at=evaluated_at,
        )
        == "active"
    )


def _evidence_override_status(
    document: ThesisDocument,
    *,
    review: IndependentReview,
    evaluated_at: datetime,
) -> Literal["absent", "invalid", "active", "expired"]:
    override = document.human_evidence_override
    if override is None:
        return "absent"
    bindings_valid = (
        document.judgment.proposed_at <= review.reviewed_at <= override.approved_at
        and override.proposal_sha256 == thesis_core_hash(document)
        and override.review_id == review.review_id
        and override.review_sha256 == independent_review_hash(review)
        and document.judgment.sizing_action == "reduced"
    )
    if not bindings_valid or evaluated_at < override.approved_at:
        return "invalid"
    if evaluated_at >= override.expires_at:
        return "expired"
    return "active"


def _check_review(
    review: IndependentReview,
    expected_hash: str,
    input_snapshot: InputSnapshot,
    judgment: JudgmentNamespace,
    evaluated_at: datetime,
    source_ids: set[str],
    required_source_ids: set[str],
    scenarios: tuple[ScenarioResult, ...],
    errors: list[str],
    warnings: list[str],
) -> None:
    if review.reviewed_thesis_sha256 != expected_hash:
        errors.append("independent review hash does not match thesis")
    if not review.checked_source_ids:
        errors.append("independent review must check at least one source")
    unknown = sorted(set(review.checked_source_ids) - source_ids)
    if unknown:
        errors.append(f"independent review references unknown sources: {unknown}")
    unchecked = sorted(required_source_ids - set(review.checked_source_ids))
    if unchecked:
        errors.append(f"independent review did not check load-bearing sources: {unchecked}")
    source_tiers = {source.source_id: source.source_tier for source in input_snapshot.sources}
    if review.primary_source_check == "verified" and not any(
        source_tiers.get(source_id) == "primary" for source_id in review.checked_source_ids
    ):
        errors.append("verified primary-source review must check a primary source")
    if review.reviewed_at.date() < input_snapshot.as_of:
        errors.append("independent review cannot predate thesis as_of")
    if review.reviewed_at < judgment.proposed_at:
        errors.append("independent review cannot predate the AI proposal")
    if review.reviewed_at > evaluated_at:
        errors.append("independent review cannot be future-dated")
    expected = {(item.horizon_years, item.name): item.total_return_cagr_pct for item in scenarios}
    supplied = {
        (item.horizon_years, item.name): item.total_return_cagr_pct
        for item in review.recalculated_scenarios
    }
    if len(supplied) != len(review.recalculated_scenarios) or supplied != expected:
        errors.append("independent review scenario recalculation does not match thesis")
    if review.primary_source_check != "verified":
        warnings.append("independent review did not fully verify primary sources")
    if review.alternative_candidate_check != "compared":
        warnings.append("independent review did not compare an alternative candidate")
    if review.proposal_changed:
        errors.append("independent review changed the proposal; regenerate the thesis")
