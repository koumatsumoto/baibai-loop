"""銘柄評価の構造と、公開前の source・単位・独立検証への参照を確認する。"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import (
    AliasChoices,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from baibai_engine.data_collection.identifiers import normalize_ticker
from baibai_engine.primitives.json import canonical_json
from baibai_engine.primitives.numbers import decimal_to_number, finite_decimal
from baibai_engine.primitives.time import JST
from baibai_engine.storage.yaml import safe_load
from baibai_engine.investment_judgment.scenario import project_return, valuation_conditions
from baibai_engine.investment_judgment.valuation import Valuation
from baibai_engine.market.sessions import latest_eligible_close_date

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
RiskAxis = Literal[
    "funding_liquidity",
    "debt_repayment",
    "cash_flow",
    "dilution",
    "customer_concentration",
    "structural_decline",
    "governance_accounting",
]


NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _normalize_ticker(value: object) -> object:
    return normalize_ticker(value) if isinstance(value, str) else value


Ticker = Annotated[str, BeforeValidator(_normalize_ticker), Field(pattern=_TICKER)]
_SHA256 = r"^[0-9a-f]{64}$"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
AssessmentId = Annotated[str, Field(pattern=rf"^stock-assessment-{_UUID}$")]
AssessmentReviewId = Annotated[str, Field(pattern=rf"^assessment-review-{_UUID}$")]


class AssessmentError(ValueError):
    """Raised when an assessment cannot be evaluated without inventing facts."""


def _date(value: object) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValueError("must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("must be an ISO date string") from error


def _datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("must include a timezone")
        return value
    if not isinstance(value, str):
        raise ValueError("must be an ISO datetime string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("must be an ISO datetime string") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("must include a timezone")
    return parsed


def _tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


class Source(BaseModel):
    model_config = _CONFIG

    source_id: NonEmptyStr
    ticker: Ticker
    source_tier: Literal["primary", "secondary", "local_data"]
    ref: NonEmptyStr | None = None
    provider: NonEmptyStr | None = None
    dataset: NonEmptyStr | None = None
    retrieved_at: AwareDatetime
    as_of: date
    used_for: NonEmptyStr

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

    fact_id: NonEmptyStr
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
    unit: NonEmptyStr
    as_of: date
    source_ids: tuple[NonEmptyStr, ...]
    observed_at: AwareDatetime | None = None
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
            if self.price_basis is None:
                raise ValueError("market_price requires price_basis")
            if self.price_basis == "realtime" and self.observed_at is None:
                raise ValueError("realtime market_price requires observed_at")
        elif self.observed_at is not None or self.price_basis is not None:
            raise ValueError("observed_at and price_basis are reserved for market_price")
        return self


class InputSnapshot(BaseModel):
    model_config = _CONFIG

    snapshot_version: Literal[1]
    producer_model_version: NonEmptyStr
    ticker: Ticker
    company_name: NonEmptyStr
    sector: NonEmptyStr
    common_factors: tuple[NonEmptyStr, ...]
    as_of: date
    sources: tuple[Source, ...]
    facts: tuple[ObservedFact, ...]

    @field_validator("snapshot_version", mode="before")
    @classmethod
    def _strict_version(cls, value: object) -> object:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("snapshot_version must be an integer")
        return value

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

    metric_id: NonEmptyStr
    value: float
    unit: NonEmptyStr
    as_of: date
    formula: Literal["ratio"]
    formula_version: Literal["ratio-v1"]
    input_fact_ids: tuple[NonEmptyStr, NonEmptyStr]
    assumption: NonEmptyStr
    source_ids: tuple[NonEmptyStr, ...]

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


class PermanentLossRisk(BaseModel):
    model_config = _CONFIG

    axis: RiskAxis
    assessment: Literal["acceptable", "adverse", "unknown"]
    evidence_status: Literal["verified", "partially_verified", "unverified"]
    summary: NonEmptyStr
    as_of: date
    source_ids: tuple[NonEmptyStr, ...]

    @field_validator("as_of", mode="before")
    @classmethod
    def _parse_date(cls, value: object) -> date:
        return _date(value)

    @field_validator("source_ids", mode="before")
    @classmethod
    def _parse_sources(cls, value: object) -> object:
        return _tuple(value)


class InvestmentCase(BaseModel):
    model_config = _CONFIG
    explanation: NonEmptyStr
    invalidation_conditions: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]
    status: Literal["intact", "broken", "uncertain"]
    status_reason: NonEmptyStr
    source_ids: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]

    @field_validator("invalidation_conditions", "source_ids", mode="before")
    @classmethod
    def _sequences(cls, value: object) -> object:
        return _tuple(value)


class AssessmentConclusion(BaseModel):
    model_config = _CONFIG
    stance: Literal["supported", "uncertain", "unsupported"]
    assessed_at: AwareDatetime
    strongest_countercase: NonEmptyStr

    @field_validator("assessed_at", mode="before")
    @classmethod
    def _time(cls, value: object) -> datetime:
        return _datetime(value)


class ValueRealization(BaseModel):
    model_config = _CONFIG
    mechanism: NonEmptyStr
    timing: Literal["known", "uncertain"]
    timing_reason: NonEmptyStr
    delay_risk: NonEmptyStr


class HypothesisCheck(BaseModel):
    model_config = _CONFIG
    statement: NonEmptyStr
    origin: Literal["user", "research"]
    result: Literal["supported", "refuted", "unresolved"]
    reason: NonEmptyStr
    source_ids: tuple[NonEmptyStr, ...]

    @field_validator("source_ids", mode="before")
    @classmethod
    def _sequences(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def _resolved_evidence(self) -> HypothesisCheck:
        if self.result != "unresolved" and not self.source_ids:
            raise ValueError("resolved hypothesis requires source references")
        return self


class ResearchFindings(BaseModel):
    model_config = _CONFIG
    business_understanding: NonEmptyStr
    value_gap_explanation: NonEmptyStr
    realization: ValueRealization
    hypotheses: tuple[HypothesisCheck, ...] = ()
    unresolved_questions: tuple[NonEmptyStr, ...] = ()
    source_ids: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]

    @field_validator("hypotheses", "unresolved_questions", "source_ids", mode="before")
    @classmethod
    def _sequences(cls, value: object) -> object:
        return _tuple(value)


class AssessmentRef(BaseModel):
    model_config = _CONFIG
    assessment_id: AssessmentId
    core_sha256: Annotated[str, Field(pattern=_SHA256)]
    review_id: AssessmentReviewId


class AssessmentRefresh(BaseModel):
    model_config = _CONFIG
    prior_assessment_ref: AssessmentRef
    checked_at: AwareDatetime
    material_information_check: NonEmptyStr
    changed_sections: tuple[
        Literal["findings", "valuation", "investment_case", "risks", "sources"], ...
    ]
    unchanged_assumptions_reason: NonEmptyStr
    horizon_reason: NonEmptyStr
    distribution_reason: NonEmptyStr
    corporate_action_reason: NonEmptyStr

    @field_validator("checked_at", mode="before")
    @classmethod
    def _time(cls, value: object) -> datetime:
        return _datetime(value)

    @field_validator("changed_sections", mode="before")
    @classmethod
    def _sequences(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def _unique_sections(self) -> AssessmentRefresh:
        if len(set(self.changed_sections)) != len(self.changed_sections):
            raise ValueError("changed_sections must be unique")
        return self


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


class StockAssessmentDocument(BaseModel):
    model_config = _CONFIG
    kind: Literal["stock_assessment"] = "stock_assessment"
    schema_version: Literal[1] = 1
    input_snapshot: InputSnapshot
    derived: DerivedNamespace
    valuation: Valuation
    investment_case: InvestmentCase
    permanent_loss_risks: tuple[PermanentLossRisk, ...]
    findings: ResearchFindings
    conclusion: AssessmentConclusion
    refresh: AssessmentRefresh | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def _strict_version(cls, value: object) -> object:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("schema_version must be an integer")
        return value

    @field_validator("permanent_loss_risks", mode="before")
    @classmethod
    def _risks(cls, value: object) -> object:
        return _tuple(value)


def current_price_projection(
    document: StockAssessmentDocument,
    *,
    price_yen: Decimal | None,
    price_as_of: date | None,
    as_of: date,
    basis_confirmed: bool,
    max_quote_age_days: int,
    price_basis: str = "last_close_unadjusted",
) -> dict[str, object] | None:
    """現在価格の見返りを見せる。原評価・期間・将来分配を変更せず、売買判定にしない。"""
    valuation = document.valuation
    if (
        not basis_confirmed
        or price_basis not in {"realtime", "last_close_unadjusted"}
        or document.input_snapshot.as_of != as_of
        or valuation.status != "resolved"
        or valuation.horizon_months is None
        or valuation.base is None
        or valuation.downside is None
        or price_yen is None
        or price_as_of is None
        or not 0 <= (as_of - price_as_of).days <= max_quote_age_days
    ):
        return None
    result: dict[str, object] = {
        "valuation_as_of": document.input_snapshot.as_of.isoformat(),
        "price_as_of": price_as_of.isoformat(),
        "price_basis": price_basis,
        "price_yen": decimal_to_number(price_yen),
        "horizon_months": valuation.horizon_months,
        "return_basis": "conditional_pretax_without_reinvestment",
    }
    for name, projection in (("base", valuation.base), ("downside", valuation.downside)):
        returns = project_return(
            projection, price_yen=price_yen, horizon_months=valuation.horizon_months
        )
        result[name] = {
            "terminal_value_per_share_yen": decimal_to_number(
                projection.terminal_value_per_share_yen
            ),
            "cash_distribution_per_share_yen": decimal_to_number(
                projection.cash_distribution_per_share_yen
            ),
            "total_value_yen": decimal_to_number(returns.total_value_yen),
            "total_return_pct": float(round(returns.total_return_pct, 4)),
            "annualized_return_pct": float(round(returns.annualized_return_pct, 4)),
        }
    return result


class UnpublishedAssessment(Enum):
    DRAFT = "draft"


type AssessmentIdentity = str | UnpublishedAssessment


@dataclass(frozen=True, slots=True)
class AssessmentEvaluation:
    assessment_status: Literal["incomplete", "review_required", "ready", "ready_with_warnings"]
    assessment_readiness: Literal["not_ready", "ready"]
    assessment_sha256: str
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


def assessment_core_hash(document: StockAssessmentDocument) -> str:
    encoded = canonical_json(document.model_dump(mode="json"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def require_recorded_identity(value: object, assessment_id: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise AssessmentError(f"assessment revision has no recorded identity: {assessment_id}")
    return value


def evaluation_to_payload(result: AssessmentEvaluation) -> dict[str, object]:
    return {
        "assessment_status": result.assessment_status,
        "assessment_readiness": result.assessment_readiness,
        "assessment_sha256": result.assessment_sha256,
        "errors": list(result.errors),
        "warnings": list(result.warnings),
    }


def assessment_valuation_context(
    document: StockAssessmentDocument, evaluation: AssessmentEvaluation
) -> dict[str, object] | None:
    """検証済み原Assessmentの成立条件を見せる。現在quote・売買判定・保存には使わない。"""
    valuation = document.valuation
    if evaluation.assessment_status not in {"ready", "ready_with_warnings", "review_required"} or (
        valuation.status != "resolved"
    ):
        return None
    assert valuation.horizon_months is not None
    assert valuation.required_annual_return_pct is not None
    assert valuation.base is not None
    assert valuation.downside is not None
    price_fact = next(
        fact
        for fact in document.input_snapshot.facts
        if fact.fact_id == valuation.market_price_fact_id
    )
    price = finite_decimal(price_fact.value)
    result: dict[str, object] = {
        "price_context": "assessment_snapshot",
        "valuation_as_of": document.input_snapshot.as_of.isoformat(),
        "price_as_of": price_fact.as_of.isoformat(),
        "price_basis": price_fact.price_basis,
        "price_yen": _valuation_context_number(price),
        "horizon_months": valuation.horizon_months,
        "required_annual_return_pct": _valuation_context_number(
            valuation.required_annual_return_pct
        ),
        "return_basis": "conditional_pretax_without_reinvestment",
    }
    delay: dict[str, object] = {
        "additional_months": 12,
        "horizon_months": valuation.horizon_months + 12,
        "assumption": "terminal_and_cumulative_cash_unchanged",
    }
    for name, projection in (("base", valuation.base), ("downside", valuation.downside)):
        conditions = valuation_conditions(
            projection,
            price_yen=price,
            horizon_months=valuation.horizon_months,
            required_annual_return_pct=valuation.required_annual_return_pct,
        )
        result["required_total_value_yen"] = _valuation_context_number(
            conditions.required_total_value_yen
        )
        result[name] = {
            "terminal_value_per_share_yen": _valuation_context_number(
                projection.terminal_value_per_share_yen
            ),
            "cash_distribution_per_share_yen": _valuation_context_number(
                projection.cash_distribution_per_share_yen
            ),
            "total_value_yen": _valuation_context_number(conditions.returns.total_value_yen),
            "total_return_pct": _valuation_context_number(
                round(conditions.returns.total_return_pct, 4)
            ),
            "annualized_return_pct": _valuation_context_number(
                round(conditions.returns.annualized_return_pct, 4)
            ),
            "required_terminal_value_per_share_yen": _valuation_context_number(
                conditions.required_terminal_value_per_share_yen
            ),
            "total_value_surplus_yen": _valuation_context_number(
                conditions.total_value_surplus_yen
            ),
        }
        delay[f"{name}_annualized_return_pct"] = _valuation_context_number(
            round(conditions.delayed_returns.annualized_return_pct, 4)
        )
    result["fixed_value_delay"] = delay
    return result


def _valuation_context_number(value: Decimal) -> int | float:
    number = decimal_to_number(finite_decimal(value))
    if isinstance(number, int):
        # YAML renders integers as text; keep its numeric conversion limit inside diagnostics.
        str(number)
    if isinstance(number, float) and not math.isfinite(number):
        raise ValueError("valuation context number is not finite after output conversion")
    return number


def load_assessment(path: Path) -> StockAssessmentDocument:
    """Load a strict YAML assessment."""

    try:
        raw = safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise AssessmentError(f"failed to load assessment: {error}") from error
    if not isinstance(raw, Mapping):
        raise AssessmentError("assessment root must be a mapping")
    try:
        return StockAssessmentDocument.model_validate(raw)
    except ValidationError as error:
        raise AssessmentError(str(error)) from error


def load_assessment_review(path: Path) -> AssessmentReview:
    """Load an Assessment Review independently from its reviewed Assessment."""

    try:
        raw = safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise AssessmentError(f"failed to load Assessment Review: {error}") from error
    if not isinstance(raw, Mapping):
        raise AssessmentError("Assessment Review root must be a mapping")
    try:
        return AssessmentReview.model_validate(raw)
    except ValidationError as error:
        raise AssessmentError(str(error)) from error


def evaluate_assessment(
    document: StockAssessmentDocument,
    *,
    identity: AssessmentIdentity,
    review: AssessmentReview | None = None,
    now: datetime,
) -> AssessmentEvaluation:
    """Check publication content; never evaluate current price, cash or buy policy.

    Source/number checks do not prove financial plausibility or truthful authoring.
    Independent Review owns that assessment, including materiality of unknowns.
    """
    instant = now
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise AssessmentError("evaluation instant must include a timezone")
    errors: list[str] = []
    warnings: list[str] = []
    snapshot = document.input_snapshot
    proposed = document.conclusion.assessed_at
    if (
        snapshot.as_of > instant.astimezone(JST).date()
        or proposed > instant
        or proposed.astimezone(JST).date() < snapshot.as_of
    ):
        errors.append("invalid assessment/as_of time")
    sources = {source.source_id: source for source in snapshot.sources}
    if not sources or len(sources) != len(snapshot.sources):
        errors.append("input_snapshot requires unique sources")
    for source in snapshot.sources:
        if source.ticker != snapshot.ticker:
            errors.append(f"source {source.source_id} ticker mismatch")
        if (
            source.as_of > snapshot.as_of
            or source.retrieved_at > proposed
            or source.retrieved_at.astimezone(JST).date() < source.as_of
        ):
            errors.append(f"source {source.source_id} has invalid time")
    facts = {fact.fact_id: fact for fact in snapshot.facts}
    if len(facts) != len(snapshot.facts):
        errors.append("fact_id must be unique")
    required_sources: set[str] = set()
    items: tuple[
        ObservedFact | DerivedMetric | PermanentLossRisk | InvestmentCase | ResearchFindings, ...
    ] = (
        *snapshot.facts,
        *document.derived.metrics,
        *document.permanent_loss_risks,
        document.investment_case,
        document.findings,
    )
    for item in items:
        refs = set(item.source_ids)
        required_sources.update(refs)
        if not refs or not refs.issubset(sources):
            errors.append("unknown or missing source reference")
        if hasattr(item, "as_of") and item.as_of > snapshot.as_of:
            errors.append("fact/metric/risk is after assessment as_of")
    for hypothesis in document.findings.hypotheses:
        refs = set(hypothesis.source_ids)
        required_sources.update(refs)
        if not refs.issubset(sources):
            errors.append("hypothesis references unknown source")
    refresh = document.refresh
    if refresh is not None and (
        refresh.checked_at > proposed or refresh.checked_at.astimezone(JST).date() < snapshot.as_of
    ):
        errors.append("refresh check must be on/after as_of and before assessment")
    for fact in snapshot.facts:
        if any(sources[ref].as_of < fact.as_of for ref in fact.source_ids if ref in sources):
            errors.append(f"fact {fact.fact_id} postdates its source")
        if fact.fact_kind == "market_price":
            if fact.unit != "JPY_per_share" or fact.price_basis not in {
                "realtime",
                "last_close_unadjusted",
            }:
                errors.append("market_price requires unadjusted JPY_per_share basis")
            if isinstance(fact.value, (bool, str)) or finite_decimal(fact.value) <= 0:
                errors.append("market_price must be a positive number")
            if fact.observed_at is not None and (
                fact.observed_at.astimezone(JST).date() != fact.as_of or fact.observed_at > proposed
            ):
                errors.append("market_price observation time mismatch")
            if (
                fact.price_basis == "last_close_unadjusted"
                and fact.as_of > latest_eligible_close_date(proposed)
            ):
                errors.append("market_price close date follows the available session")
        if fact.fact_kind == "valuation_metric" and (
            fact.unit not in {"ratio", "percent", "JPY_per_share"}
            or isinstance(fact.value, (bool, str))
        ):
            errors.append("valuation_metric numeric/unit mismatch")
    valuation = document.valuation
    if valuation.market_price_fact_id is not None:
        price_fact = facts.get(valuation.market_price_fact_id)
        if price_fact is None or price_fact.fact_kind != "market_price":
            errors.append("valuation price fact does not resolve")
    for projection in (valuation.base, valuation.downside):
        if projection is not None:
            required_sources.update(projection.source_ids)
            if not set(projection.source_ids).issubset(sources):
                errors.append("projection references unknown source")
    axes = [risk.axis for risk in document.permanent_loss_risks]
    if len(axes) != len(_RISK_AXES) or set(axes) != _RISK_AXES:
        errors.append("all seven permanent-loss risk axes are required exactly once")
    gaps = [
        risk.axis
        for risk in document.permanent_loss_risks
        if risk.assessment == "unknown" or risk.evidence_status != "verified"
    ]
    if gaps:
        warnings.append(f"permanent-loss evidence incomplete: {gaps}")
    case = document.investment_case
    stance = document.conclusion.stance
    if case.status == "broken" and stance != "unsupported":
        errors.append("broken investment case requires unsupported")
    if case.status == "uncertain" and stance == "supported":
        errors.append("uncertain investment case cannot be supported")
    if stance == "supported" and valuation.status != "resolved":
        errors.append("supported requires resolved valuation")
    _check_derived_metrics(document, errors)
    core_hash = assessment_core_hash(document)
    if identity is not UnpublishedAssessment.DRAFT and identity != core_hash:
        errors.append("recorded Assessment hash does not match assessment")
    if review is None:
        errors.append("Reviewed Assessment requires an Assessment Review")
    else:
        if review.reviewed_assessment_sha256 != core_hash:
            errors.append("Assessment Review hash does not match assessment")
        if not proposed <= review.reviewed_at <= instant:
            errors.append("Assessment Review time must follow assessment and not be future-dated")
        if not set(review.checked_source_ids).issubset(sources) or not required_sources.issubset(
            review.checked_source_ids
        ):
            errors.append("Assessment Review source coverage mismatch")
        primary_checked = any(
            sources[ref].source_tier == "primary"
            for ref in review.checked_source_ids
            if ref in sources
        )
        if review.primary_source_check == "verified" and not primary_checked:
            errors.append("verified review must check a primary source")
        if stance == "supported" and review.primary_source_check != "verified":
            errors.append("supported requires verified material primary evidence")
        if (
            stance == "supported"
            and gaps
            and not (review.uncertainty_acceptance_reason or "").strip()
        ):
            errors.append("supported with unknowns requires Review's uncertainty acceptance reason")
        checked: dict[str, ReviewedProjection] = {
            item.name: item for item in review.recalculated_projections
        }
        expected = {"base", "downside"} if valuation.status == "resolved" else set()
        if set(checked) != expected or len(checked) != len(review.recalculated_projections):
            errors.append("Review requires exactly Base/Downside for resolved valuation")
        for name, original in (("base", valuation.base), ("downside", valuation.downside)):
            recalculated = checked.get(name)
            if original is not None and recalculated is not None:
                for field in ("terminal_value_per_share_yen", "cash_distribution_per_share_yen"):
                    if abs(getattr(original, field) - getattr(recalculated, field)) > Decimal(
                        "0.0001"
                    ):
                        errors.append(f"Review {name} {field} mismatch")
    status: Literal["incomplete", "review_required", "ready", "ready_with_warnings"] = (
        "ready_with_warnings" if warnings else "ready"
    )
    if errors:
        status = (
            "review_required"
            if errors == ["Reviewed Assessment requires an Assessment Review"]
            else "incomplete"
        )
    return AssessmentEvaluation(
        status, "not_ready" if errors else "ready", core_hash, tuple(errors), tuple(warnings)
    )


def ratio_value_from_facts(
    *,
    metric_id: str,
    input_fact_ids: tuple[str, str],
    unit: str,
    facts: Mapping[str, ObservedFact],
) -> tuple[Decimal | None, list[str]]:
    """One ratio-v1 calculation, shared by generation and the unchanged checks."""
    errors: list[str] = []
    inputs = [facts.get(fact_id) for fact_id in input_fact_ids]
    if any(item is None for item in inputs):
        errors.append(f"metric {metric_id} references unknown input fact")
        return None, errors
    values: list[Decimal] = []
    units: list[str] = []
    for item in inputs:
        assert item is not None
        if isinstance(item.value, bool | str):
            errors.append(f"metric {metric_id} input facts must be numeric")
            break
        value = Decimal(str(item.value))
        if abs(value) > Decimal("1e30"):
            errors.append(f"metric {metric_id} input fact magnitude is too large")
            break
        values.append(value)
        units.append(item.unit)
    if len(values) != 2:
        return None, errors
    if values[1] == 0:
        errors.append(f"metric {metric_id} ratio denominator cannot be zero")
        return None, errors
    expected = values[0] / values[1]
    if abs(expected) > Decimal("1e300"):
        errors.append(f"metric {metric_id} ratio result magnitude is too large")
        return None, errors
    expected_unit = {
        ("JPY", "shares"): "JPY_per_share",
        ("JPY", "JPY"): "ratio",
        ("shares", "shares"): "ratio",
    }.get((units[0], units[1]))
    if expected_unit is None or unit != expected_unit:
        errors.append(f"metric {metric_id} unit mismatch for ratio inputs {tuple(units)}")
    return expected, errors


def _check_derived_metrics(document: StockAssessmentDocument, errors: list[str]) -> None:
    facts = {fact.fact_id: fact for fact in document.input_snapshot.facts}
    metric_ids = {metric.metric_id for metric in document.derived.metrics}
    if len(metric_ids) != len(document.derived.metrics):
        errors.append("derived metric_id must be unique")
    for metric in document.derived.metrics:
        expected, ratio_errors = ratio_value_from_facts(
            metric_id=metric.metric_id,
            input_fact_ids=metric.input_fact_ids,
            unit=metric.unit,
            facts=facts,
        )
        errors.extend(ratio_errors)
        if expected is None:
            continue
        if not math.isclose(metric.value, float(expected), rel_tol=1e-12, abs_tol=1e-12):
            errors.append(
                f"metric {metric.metric_id} value mismatch: expected {expected}, got {metric.value}"
            )
