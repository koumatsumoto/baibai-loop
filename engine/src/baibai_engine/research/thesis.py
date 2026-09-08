"""企業評価を産み、公開前に source・単位・独立検算の不整合を止める。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.research.decimal_number import decimal_to_number
from baibai_engine.research.valuation import (
    Projection,
    finite_decimal,
    project_return,
    valuation_conditions,
)

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


class InvestmentCase(BaseModel):
    model_config = _CONFIG
    explanation: Annotated[str, Field(min_length=1, pattern=r"\S")]
    invalidation_conditions: Annotated[tuple[str, ...], Field(min_length=1)]
    status: Literal["intact", "broken", "uncertain"]
    status_reason: Annotated[str, Field(min_length=1, pattern=r"\S")]
    source_ids: Annotated[tuple[str, ...], Field(min_length=1)]

    @field_validator("invalidation_conditions", "source_ids", mode="before")
    @classmethod
    def _sequences(cls, value: object) -> object:
        return _tuple(value)


class Valuation(BaseModel):
    model_config = _CONFIG
    status: Literal["resolved", "unresolved"]
    market_price_fact_id: str | None
    horizon_months: Annotated[int, Field(gt=0)] | None
    required_annual_return_pct: Annotated[Decimal, Field(gt=0)] | None
    base: Projection | None
    downside: Projection | None
    unresolved_reason: str | None

    @field_validator("required_annual_return_pct", mode="before")
    @classmethod
    def _rate(cls, value: object) -> Decimal | None:
        return None if value is None else finite_decimal(value)

    @model_validator(mode="after")
    def _resolution(self) -> Valuation:
        values = (self.horizon_months, self.required_annual_return_pct, self.base, self.downside)
        if self.status == "resolved":
            if any(value is None for value in values) or not self.market_price_fact_id:
                raise ValueError("resolved valuation requires price, horizon, rate and projections")
            if self.unresolved_reason is not None:
                raise ValueError("resolved valuation forbids unresolved_reason")
        elif (
            any(value is not None for value in values) or not (self.unresolved_reason or "").strip()
        ):
            raise ValueError(
                "unresolved valuation requires a reason and null projections/horizon/rate"
            )
        return self


class JudgmentNamespace(BaseModel):
    model_config = _CONFIG
    disposition: Literal["candidate", "defer", "reject"]
    proposed_at: datetime
    strongest_countercase: Annotated[str, Field(min_length=1, pattern=r"\S")]

    @field_validator("proposed_at", mode="before")
    @classmethod
    def _time(cls, value: object) -> datetime:
        return _datetime(value)


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


class ThesisReview(BaseModel):
    model_config = _CONFIG
    review_id: Annotated[str, Field(min_length=1)]
    reviewer_role: Literal["independent_second_pass"]
    reviewer_identity: Annotated[str, Field(min_length=1, pattern=r"\S")]
    reviewed_at: datetime
    reviewed_thesis_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    primary_source_check: Literal["verified", "partially_verified", "unverified"]
    checked_source_ids: Annotated[tuple[str, ...], Field(min_length=1)]
    recalculated_projections: tuple[ReviewedProjection, ...]
    strongest_countercase: Annotated[str, Field(min_length=1, pattern=r"\S")]
    nonmaterial_unknown_reason: str | None = None

    @field_validator("reviewed_at", mode="before")
    @classmethod
    def _time(cls, value: object) -> datetime:
        return _datetime(value)

    @field_validator("checked_source_ids", "recalculated_projections", mode="before")
    @classmethod
    def _sequences(cls, value: object) -> object:
        return _tuple(value)


class ThesisDocument(BaseModel):
    model_config = _CONFIG
    schema_version: Literal[4]
    input_snapshot: InputSnapshot
    derived: DerivedNamespace
    valuation: Valuation
    investment_case: InvestmentCase
    permanent_loss_risks: tuple[PermanentLossRisk, ...]
    judgment: JudgmentNamespace

    @field_validator("permanent_loss_risks", mode="before")
    @classmethod
    def _risks(cls, value: object) -> object:
        return _tuple(value)


def current_price_projection(
    document: ThesisDocument,
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


class UnpublishedThesis(Enum):
    DRAFT = "draft"


type ThesisIdentity = str | UnpublishedThesis


@dataclass(frozen=True, slots=True)
class ThesisEvaluation:
    thesis_status: Literal["incomplete", "review_required", "ready", "ready_with_warnings"]
    decision_readiness: Literal["not_ready", "ready"]
    thesis_sha256: str
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


def thesis_core_hash(document: ThesisDocument) -> str:
    encoded = json.dumps(
        document.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def require_recorded_identity(value: object, thesis_id: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ThesisError(f"thesis revision has no recorded identity: {thesis_id}")
    return value


def evaluation_to_payload(result: ThesisEvaluation) -> dict[str, object]:
    return {
        "thesis_status": result.thesis_status,
        "decision_readiness": result.decision_readiness,
        "thesis_sha256": result.thesis_sha256,
        "errors": list(result.errors),
        "warnings": list(result.warnings),
    }


def thesis_valuation_context(
    document: ThesisDocument, evaluation: ThesisEvaluation
) -> dict[str, object] | None:
    """検証済み原Thesisの成立条件を見せる。現在quote・売買判定・保存には使わない。"""
    valuation = document.valuation
    if evaluation.thesis_status not in {"ready", "ready_with_warnings", "review_required"} or (
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
        "price_context": "thesis_snapshot",
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


def load_thesis_review(path: Path) -> ThesisReview:
    """Load a Thesis Review independently from its reviewed Thesis."""

    try:
        raw = safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ThesisError(f"failed to load Thesis Review: {error}") from error
    if not isinstance(raw, Mapping):
        raise ThesisError("Thesis Review root must be a mapping")
    try:
        return ThesisReview.model_validate(raw)
    except ValidationError as error:
        raise ThesisError(str(error)) from error


def evaluate_thesis(
    document: ThesisDocument,
    *,
    identity: ThesisIdentity,
    review: ThesisReview | None = None,
    now: datetime | None = None,
) -> ThesisEvaluation:
    """Check publication content; never evaluate current price, cash or buy policy.

    Source/number checks do not prove financial plausibility or truthful authoring.
    Independent Review owns that assessment, including materiality of unknowns.
    """
    instant = now or datetime.now(ZoneInfo("Asia/Tokyo"))
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ThesisError("evaluation instant must include a timezone")
    errors: list[str] = []
    warnings: list[str] = []
    snapshot = document.input_snapshot
    proposed = document.judgment.proposed_at
    if snapshot.as_of > instant.date() or proposed > instant or proposed.date() < snapshot.as_of:
        errors.append("invalid proposal/as_of time")
    sources = {source.source_id: source for source in snapshot.sources}
    if not sources or len(sources) != len(snapshot.sources):
        errors.append("input_snapshot requires unique sources")
    for source in snapshot.sources:
        if source.ticker != snapshot.ticker:
            errors.append(f"source {source.source_id} ticker mismatch")
        if (
            source.as_of > snapshot.as_of
            or source.retrieved_at > proposed
            or source.retrieved_at.date() < source.as_of
        ):
            errors.append(f"source {source.source_id} has invalid time")
    facts = {fact.fact_id: fact for fact in snapshot.facts}
    if len(facts) != len(snapshot.facts):
        errors.append("fact_id must be unique")
    required_sources: set[str] = set()
    items: tuple[ObservedFact | DerivedMetric | PermanentLossRisk | InvestmentCase, ...] = (
        *snapshot.facts,
        *document.derived.metrics,
        *document.permanent_loss_risks,
        document.investment_case,
    )
    for item in items:
        refs = set(item.source_ids)
        required_sources.update(refs)
        if not refs or not refs.issubset(sources):
            errors.append("unknown or missing source reference")
        if hasattr(item, "as_of") and item.as_of > snapshot.as_of:
            errors.append("fact/metric/risk is after thesis as_of")
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
            if (
                fact.observed_at is None
                or fact.observed_at.date() != fact.as_of
                or fact.observed_at > proposed
            ):
                errors.append("market_price observation time mismatch")
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
    disposition = document.judgment.disposition
    if case.status == "broken" and disposition != "reject":
        errors.append("broken investment case requires reject")
    if case.status == "uncertain" and disposition == "candidate":
        errors.append("uncertain investment case cannot be candidate")
    if disposition == "candidate" and valuation.status != "resolved":
        errors.append("candidate requires resolved valuation")
    _check_derived_metrics(document, errors)
    core_hash = thesis_core_hash(document) if identity is UnpublishedThesis.DRAFT else identity
    if review is None:
        errors.append("Reviewed Thesis requires a Thesis Review")
    else:
        if review.reviewed_thesis_sha256 != core_hash:
            errors.append("Thesis Review hash does not match thesis")
        if not proposed <= review.reviewed_at <= instant:
            errors.append("Thesis Review time must follow proposal and not be future-dated")
        if not set(review.checked_source_ids).issubset(sources) or not required_sources.issubset(
            review.checked_source_ids
        ):
            errors.append("Thesis Review source coverage mismatch")
        primary_checked = any(
            sources[ref].source_tier == "primary"
            for ref in review.checked_source_ids
            if ref in sources
        )
        if review.primary_source_check == "verified" and not primary_checked:
            errors.append("verified review must check a primary source")
        if disposition == "candidate" and review.primary_source_check != "verified":
            errors.append("candidate requires verified material primary evidence")
        if (
            disposition == "candidate"
            and gaps
            and not (review.nonmaterial_unknown_reason or "").strip()
        ):
            errors.append("candidate with unknowns requires Review's nonmaterial explanation")
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
            if errors == ["Reviewed Thesis requires a Thesis Review"]
            else "incomplete"
        )
    return ThesisEvaluation(
        status, "not_ready" if errors else "ready", core_hash, tuple(errors), tuple(warnings)
    )


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
