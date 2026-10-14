"""一回切替だけが読む旧評価・旧買付参照。旧runtimeへの依存は持たない。

Thesis4の構成要素と検証は切替時の証拠検査のためここへ固定する。
新評価の作成・公開、旧提案条件の補完、旧writerの再起動はしない。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from baibai_engine.foundation.time import JST
from baibai_engine.storage.application.json import canonical_json
from baibai_engine.valuation.models import Valuation
from baibai_engine.valuation.scenario import finite_decimal

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


class _LegacyThesisError(ValueError):
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


class _LegacySource(BaseModel):
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
    def _source_locator(self) -> _LegacySource:
        if self.source_tier == "local_data":
            if self.ref is not None or self.provider is None or self.dataset is None:
                raise ValueError("local_data source requires provider and dataset and forbids ref")
        elif self.ref is None or not self.ref.startswith("https://"):
            raise ValueError("external source requires an HTTPS ref")
        elif self.provider is not None or self.dataset is not None:
            raise ValueError("external source forbids local provider and dataset")
        return self


class _LegacyObservedFact(BaseModel):
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
    def _market_price_metadata(self) -> _LegacyObservedFact:
        if self.fact_kind == "market_price":
            if self.observed_at is None or self.price_basis is None:
                raise ValueError("market_price requires observed_at and price_basis")
        elif self.observed_at is not None or self.price_basis is not None:
            raise ValueError("observed_at and price_basis are reserved for market_price")
        return self


class _LegacyInputSnapshot(BaseModel):
    model_config = _CONFIG

    snapshot_version: Literal[1]
    producer_model_version: Annotated[str, Field(min_length=1)]
    ticker: Annotated[str, Field(pattern=_TICKER)]
    company_name: Annotated[str, Field(min_length=1)]
    sector: Annotated[str, Field(min_length=1)]
    common_factors: tuple[Annotated[str, Field(min_length=1)], ...]
    as_of: date
    sources: tuple[_LegacySource, ...]
    facts: tuple[_LegacyObservedFact, ...]

    @field_validator("as_of", mode="before")
    @classmethod
    def _parse_date(cls, value: object) -> date:
        return _date(value)

    @field_validator("sources", "facts", "common_factors", mode="before")
    @classmethod
    def _parse_sequences(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def _common_factor_shape(self) -> _LegacyInputSnapshot:
        if len(set(self.common_factors)) != len(self.common_factors):
            raise ValueError("common_factors must not contain duplicates")
        if self.common_factors != tuple(sorted(self.common_factors)):
            raise ValueError("common_factors must be sorted")
        return self


class _LegacyDerivedMetric(BaseModel):
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


class _LegacyDerivedNamespace(BaseModel):
    model_config = _CONFIG

    metrics: tuple[_LegacyDerivedMetric, ...]

    @field_validator("metrics", mode="before")
    @classmethod
    def _parse_metrics(cls, value: object) -> object:
        return _tuple(value)


class _LegacyPermanentLossRisk(BaseModel):
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


class _LegacyInvestmentCase(BaseModel):
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


class _LegacyJudgmentNamespace(BaseModel):
    model_config = _CONFIG
    disposition: Literal["candidate", "defer", "reject"]
    proposed_at: datetime
    strongest_countercase: Annotated[str, Field(min_length=1, pattern=r"\S")]

    @field_validator("proposed_at", mode="before")
    @classmethod
    def _time(cls, value: object) -> datetime:
        return _datetime(value)


class _LegacyReviewedProjection(BaseModel):
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


class _LegacyThesisReview(BaseModel):
    model_config = _CONFIG
    review_id: Annotated[str, Field(min_length=1)]
    reviewer_role: Literal["independent_second_pass"]
    reviewer_identity: Annotated[str, Field(min_length=1, pattern=r"\S")]
    reviewed_at: datetime
    reviewed_thesis_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    primary_source_check: Literal["verified", "partially_verified", "unverified"]
    checked_source_ids: Annotated[tuple[str, ...], Field(min_length=1)]
    recalculated_projections: tuple[_LegacyReviewedProjection, ...]
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


class _LegacyThesisDocument(BaseModel):
    model_config = _CONFIG
    schema_version: Literal[4]
    input_snapshot: _LegacyInputSnapshot
    derived: _LegacyDerivedNamespace
    valuation: Valuation
    investment_case: _LegacyInvestmentCase
    permanent_loss_risks: tuple[_LegacyPermanentLossRisk, ...]
    judgment: _LegacyJudgmentNamespace

    @field_validator("permanent_loss_risks", mode="before")
    @classmethod
    def _risks(cls, value: object) -> object:
        return _tuple(value)


class _LegacyUnpublishedThesis(Enum):
    DRAFT = "draft"


type _LegacyThesisIdentity = str | _LegacyUnpublishedThesis


@dataclass(frozen=True, slots=True)
class _LegacyThesisEvaluation:
    thesis_status: Literal["incomplete", "review_required", "ready", "ready_with_warnings"]
    decision_readiness: Literal["not_ready", "ready"]
    thesis_sha256: str
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


def _legacy_thesis_core_hash(document: _LegacyThesisDocument) -> str:
    encoded = json.dumps(
        document.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _evaluate_legacy_thesis(
    document: _LegacyThesisDocument,
    *,
    identity: _LegacyThesisIdentity,
    review: _LegacyThesisReview | None = None,
    now: datetime | None = None,
) -> _LegacyThesisEvaluation:
    """Check publication content; never evaluate current price, cash or buy policy.

    _LegacySource/number checks do not prove financial plausibility or truthful authoring.
    Independent Review owns that assessment, including materiality of unknowns.
    """
    instant = now or datetime.now(JST)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise _LegacyThesisError("evaluation instant must include a timezone")
    errors: list[str] = []
    warnings: list[str] = []
    snapshot = document.input_snapshot
    proposed = document.judgment.proposed_at
    if (
        snapshot.as_of > instant.astimezone(JST).date()
        or proposed > instant
        or proposed.astimezone(JST).date() < snapshot.as_of
    ):
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
            or source.retrieved_at.astimezone(JST).date() < source.as_of
        ):
            errors.append(f"source {source.source_id} has invalid time")
    facts = {fact.fact_id: fact for fact in snapshot.facts}
    if len(facts) != len(snapshot.facts):
        errors.append("fact_id must be unique")
    required_sources: set[str] = set()
    items: tuple[
        _LegacyObservedFact
        | _LegacyDerivedMetric
        | _LegacyPermanentLossRisk
        | _LegacyInvestmentCase,
        ...,
    ] = (
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
                or fact.observed_at.astimezone(JST).date() != fact.as_of
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
    core_hash = (
        _legacy_thesis_core_hash(document)
        if identity is _LegacyUnpublishedThesis.DRAFT
        else identity
    )
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
        checked: dict[str, _LegacyReviewedProjection] = {
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
    return _LegacyThesisEvaluation(
        status, "not_ready" if errors else "ready", core_hash, tuple(errors), tuple(warnings)
    )


def _legacy_ratio_value_from_facts(
    *,
    metric_id: str,
    input_fact_ids: tuple[str, str],
    unit: str,
    facts: Mapping[str, _LegacyObservedFact],
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


def _check_derived_metrics(document: _LegacyThesisDocument, errors: list[str]) -> None:
    facts = {fact.fact_id: fact for fact in document.input_snapshot.facts}
    metric_ids = {metric.metric_id for metric in document.derived.metrics}
    if len(metric_ids) != len(document.derived.metrics):
        errors.append("derived metric_id must be unique")
    for metric in document.derived.metrics:
        expected, ratio_errors = _legacy_ratio_value_from_facts(
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


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonempty text")
    return value


def legacy_assessment_pair(
    snapshot: Mapping[str, list[dict[str, Any]]], identifier: str
) -> tuple[_LegacyThesisDocument, dict[str, Any], dict[str, Any]]:
    """Validate one exact source22 pair without any runtime legacy reader."""
    rows = [row for row in snapshot["thesis"] if row["thesis_id"] == identifier]
    if len(rows) != 1:
        raise ValueError(f"missing old assessment source: {identifier}")
    row = rows[0]
    reviews = [row for row in snapshot["thesis_review"] if row["thesis_id"] == identifier]
    if len(reviews) != 1:
        raise ValueError(f"old assessment requires exactly one Review: {identifier}")
    document = _LegacyThesisDocument.model_validate_json(str(row["payload"]))
    review = _LegacyThesisReview.model_validate_json(str(reviews[0]["payload"]))
    identity = _legacy_thesis_core_hash(document)
    if (
        identity != row["core_sha256"]
        or review.reviewed_thesis_sha256 != identity
        or (row["ticker"], row["as_of"], row["recommendation"])
        != (
            document.input_snapshot.ticker,
            document.input_snapshot.as_of.isoformat(),
            document.judgment.disposition,
        )
        or review.review_id != reviews[0]["review_id"]
        or review.reviewed_at != datetime.fromisoformat(reviews[0]["reviewed_at"])
    ):
        raise ValueError(f"old assessment source identity differs: {identifier}")
    evaluation = _evaluate_legacy_thesis(
        document, identity=identity, review=review, now=datetime.fromisoformat(row["published_at"])
    )
    if evaluation.errors:
        raise ValueError(
            f"invalid old assessment source {identifier}: {'; '.join(evaluation.errors)}"
        )
    return document, row, reviews[0]


def assessment_source_draft(
    snapshot: Mapping[str, list[dict[str, Any]]], identifier: str
) -> dict[str, Any]:
    """Retain useful old material, leaving new findings/time/Review unauthored."""
    source = next((row for row in snapshot["thesis"] if row["thesis_id"] == identifier), None)
    if source is not None and json.loads(source["payload"]).get("schema_version") == 3:
        return assessment3_source_draft(snapshot, identifier)
    document, source, review = legacy_assessment_pair(snapshot, identifier)
    raw = json.loads(source["payload"])
    stance = {"candidate": "supported", "defer": "uncertain", "reject": "unsupported"}[
        document.judgment.disposition
    ]
    draft = {
        field: raw[field]
        for field in (
            "input_snapshot",
            "derived",
            "valuation",
            "investment_case",
            "permanent_loss_risks",
        )
    }
    draft.update(
        kind="stock_assessment",
        schema_version=1,
        findings=None,
        conclusion={
            "stance": stance,
            "assessed_at": None,
            "strongest_countercase": document.judgment.strongest_countercase,
        },
        refresh=None,
    )
    return {
        "kind": "assessment_source_draft",
        "source_thesis_id": identifier,
        "source_core_sha256": source["core_sha256"],
        "source_published_at": source["published_at"],
        "source_document": raw,
        "source_payload": source["payload"],
        "source_review": json.loads(review["payload"]),
        "source_review_payload": review["payload"],
        "assessment_draft": draft,
        "new_review_required": True,
    }


def legacy_caa(row: Mapping[str, Any], *, require_allocate: bool = False) -> dict[str, Any]:
    """Only the original CAA proves its allocated ticker; a Fill never does."""
    raw = _object(json.loads(row["payload"]), "old CAA")
    version = raw.get("schema_version")
    if type(version) is not int or version not in {1, 2}:
        raise ValueError("old CAA source requires schema1 or schema2")
    identifier = _text(raw.get("capital_allocation_assessment_id"), "old CAA ID")
    if (
        raw.get("kind") != "capital_allocation_assessment"
        or identifier != row["capital_allocation_assessment_id"]
    ):
        raise ValueError("old CAA source identity differs")
    if not re.fullmatch(
        r"(?:capital-allocation-assessment|bargain-assessment)-\d{8}-[a-z0-9-]+", identifier
    ):
        raise ValueError("old CAA ID has an invalid format")
    if raw.get("as_of") != row["as_of"] or raw.get("result") != row["result"]:
        raise ValueError("old CAA date/result differs from source columns")
    date.fromisoformat(_text(raw.get("as_of"), "old CAA as_of"))
    published = datetime.fromisoformat(_text(raw.get("published_at"), "old CAA publication time"))
    if published.utcoffset() is None or published != datetime.fromisoformat(row["published_at"]):
        raise ValueError("old CAA publication time differs")
    if version == 1 and "market_context_assessment_id" in raw:
        raise ValueError("old CAA1 cannot carry the schema2 context reference")
    for field in ("headline", "comparison", "forgone"):
        _text(raw.get(field), f"old CAA {field}")
    alternatives = raw.get("alternatives")
    if not isinstance(alternatives, list) or not alternatives:
        raise ValueError("old CAA alternatives must be a nonempty array")
    tickers = []
    allocated = []
    for value in alternatives:
        alternative = _object(value, "old CAA alternative")
        ticker = _text(alternative.get("ticker"), "old CAA ticker")
        if re.fullmatch(_TICKER, ticker) is None:
            raise ValueError("old CAA ticker is invalid")
        tickers.append(ticker)
        _text(alternative.get("thesis_id"), "old CAA assessment reference")
        digest = _text(alternative.get("thesis_core_sha256"), "old CAA assessment digest")
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("old CAA assessment digest is invalid")
        _text(alternative.get("rationale"), "old CAA rationale")
        disposition = alternative.get("disposition")
        if disposition not in {"allocate", "decline", "defer"}:
            raise ValueError("old CAA alternative disposition is invalid")
        if disposition == "allocate":
            _text(alternative.get("thesis_review_id"), "allocated old CAA Review reference")
            allocated.append(alternative)
    if len(set(tickers)) != len(tickers):
        raise ValueError("old CAA alternatives must have unique tickers")
    result = raw.get("result")
    if result not in {"allocate", "no_allocation", "defer"}:
        raise ValueError("old CAA result is invalid")
    if (result == "allocate" and len(allocated) != 1) or (result != "allocate" and allocated):
        raise ValueError("old CAA requires exactly one allocated ticker for allocate")
    if require_allocate and result != "allocate":
        raise ValueError("retained buy reference must identify an allocate CAA")
    review = _object(raw.get("review"), "old CAA Review")
    attempt = review.get("attempt")
    if type(attempt) is not int or attempt < 1:
        raise ValueError("old CAA Review attempt must be positive")
    _text(review.get("reviewer_identity"), "old CAA reviewer")
    reviewed_at = datetime.fromisoformat(_text(review.get("reviewed_at"), "old CAA review time"))
    if reviewed_at.utcoffset() is None or reviewed_at > published:
        raise ValueError("old CAA review time is invalid")
    if review.get("conclusion") != "pass" or review.get("open_findings", []) != []:
        raise ValueError("old CAA source requires a passing Review")
    draft = {key: value for key, value in raw.items() if key not in {"review", "published_at"}}
    if review.get("draft_sha256") != hashlib.sha256(canonical_json(draft).encode()).hexdigest():
        raise ValueError("old CAA Review hash differs from source content")
    return raw


def retained_buy_binding(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = legacy_caa(row, require_allocate=True)
    allocated = next(item for item in raw["alternatives"] if item["disposition"] == "allocate")
    identifier = row["capital_allocation_assessment_id"]
    return {
        "reference": identifier,
        "ticker": allocated["ticker"],
        "side": "buy",
        "origin": "retained_reference",
        "proposal_id": None,
        "source_id": identifier,
        "source_digest": hashlib.sha256(str(row["payload"]).encode()).hexdigest(),
    }


def retained_ledger_buy_binding(
    reference: str,
    events: list[dict[str, Any]],
    caa: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Keep an explicit historical identity, without approving the old proposal.

    The digest covers the original SQL rows (including their exact payload text),
    ordered by append_seq. source_id identifies the first row in that group.
    """
    ordered = sorted(events, key=lambda row: row["append_seq"])
    tickers = set()
    for row in ordered:
        raw = _object(json.loads(row["payload"]), "old buy ledger fact")
        ticker = _text(raw.get("ticker"), "old buy ledger ticker")
        if (
            re.fullmatch(_TICKER, ticker) is None
            or raw.get("type") not in {"execution", "reservation"}
            or raw.get("side", "buy") != "buy"
            or raw.get("decision_reference") != reference
            or (row["event_id"], row["event_type"], row["ticker"], row["decision_reference"])
            != (raw.get("event_id"), raw.get("type"), ticker, reference)
        ):
            raise ValueError(f"old buy reference ledger identity differs: {reference}")
        tickers.add(ticker)
    if len(tickers) != 1:
        raise ValueError(f"old buy reference must have one ledger ticker: {reference}")
    ticker = next(iter(tickers))
    if caa is not None:
        raw = _object(json.loads(caa["payload"]), "old CAA identity")
        if (
            type(raw.get("schema_version")) is not int
            or raw.get("schema_version") not in {1, 2}
            or raw.get("kind") != "capital_allocation_assessment"
            or raw.get("capital_allocation_assessment_id") != reference
            or caa["capital_allocation_assessment_id"] != reference
            or raw.get("result") != "allocate"
            or caa["result"] != "allocate"
        ):
            raise ValueError(f"old buy reference CAA identity differs: {reference}")
        alternatives = raw.get("alternatives")
        if not isinstance(alternatives, list):
            raise ValueError(f"old buy reference CAA alternatives are missing: {reference}")
        parsed = [_object(item, "old CAA alternative") for item in alternatives]
        allocated = [item for item in parsed if item.get("disposition") == "allocate"]
        if len(allocated) != 1 or allocated[0].get("ticker") != ticker:
            raise ValueError(f"old buy reference ticker differs from ledger facts: {reference}")
    return {
        "reference": reference,
        "ticker": ticker,
        "side": "buy",
        "origin": "retained_reference",
        "proposal_id": None,
        "source_id": f"ledger-event:{ordered[0]['event_id']}",
        "source_digest": hashlib.sha256(canonical_json(ordered).encode()).hexdigest(),
    }


def position_reason_source(
    snapshot: Mapping[str, list[dict[str, Any]]], identifier: str
) -> dict[str, Any]:
    """Cite a confirmed old source, never claim that an AI proposal was adopted."""
    caa = next(
        (
            row
            for row in snapshot["capital_allocation_assessment"]
            if row["capital_allocation_assessment_id"] == identifier
        ),
        None,
    )
    candidate_id = None
    if caa is not None:
        raw = legacy_caa(caa, require_allocate=True)
        selected = next(item for item in raw["alternatives"] if item["disposition"] == "allocate")
        verify_alternative_source(snapshot, selected)
        thesis_id, ticker = selected["thesis_id"], selected["ticker"]
    else:
        row = next(
            (row for row in snapshot["position_review"] if row["position_review_id"] == identifier),
            None,
        )
        if row is None:
            raise ValueError(f"missing old position reason source: {identifier}")
        raw = _object(json.loads(row["payload"]), "old Position Review")
        if raw.get("ticker") != row["ticker"] or raw.get("as_of") != row["as_of"]:
            raise ValueError("old Position Review source identity differs")
        if raw.get("schema_version") == 2:
            sources = _object(raw.get("sources"), "old Position Review sources")
            holding = _object(sources.get("holding_thesis"), "old holding Thesis source")
            ledger = _object(sources.get("ledger"), "old Position Review ledger source")
            head = ledger.get("append_head")
            if (
                holding.get("entity_id") != row["thesis_id"]
                or ledger.get("entity_id") != "portfolio-ledger"
                or type(head) is not int
                or not 0
                <= head
                <= max((event["append_seq"] for event in snapshot["ledger_event"]), default=0)
            ):
                raise ValueError("old Position Review canonical source identity differs")
            candidate = sources.get("candidate_thesis")
            candidate_id = row["candidate_thesis_id"]
            if (candidate is None) != (candidate_id is None) or (
                candidate is not None
                and _object(candidate, "old candidate Thesis source").get("entity_id")
                != candidate_id
            ):
                raise ValueError("old Position Review candidate source identity differs")
            if candidate_id is not None:
                candidate_source = next(
                    (item for item in snapshot["thesis"] if item["thesis_id"] == candidate_id),
                    None,
                )
                replacement = _object(
                    raw.get("replacement_comparison"), "old replacement comparison"
                )
                candidate_terms = _object(replacement.get("candidate"), "old replacement candidate")
                if (
                    candidate_source is None
                    or replacement.get("status") != "evaluated"
                    or candidate_terms.get("ticker") != candidate_source["ticker"]
                ):
                    raise ValueError("old Position Review replacement ticker/source differs")
                # Validate the referenced historical material, without adopting its terms.
                assessment_source_draft(snapshot, candidate_id)
            thesis_id = _text(holding.get("entity_id"), "old position assessment ID")
        elif (
            raw.get("schema_version") == 3
            and raw.get("position_review_id") == identifier
            and raw.get("thesis_id") == row["thesis_id"]
        ):
            thesis_id = _text(raw.get("thesis_id"), "old position assessment ID")
        else:
            raise ValueError("old Position Review source identity differs")
        ticker = _text(raw.get("ticker"), "old position ticker")
    source = next((row for row in snapshot["thesis"] if row["thesis_id"] == thesis_id), None)
    if source is None:
        raise ValueError(f"missing old position assessment source: {thesis_id}")
    limitations: list[str] = []
    if json.loads(source["payload"]).get("schema_version") == 3:
        older, source, _review, limitations = _legacy3_pair(snapshot, thesis_id)
        source_ticker, explanation = older.input_snapshot.ticker, None
    else:
        document, source, _review = legacy_assessment_pair(snapshot, thesis_id)
        source_ticker, explanation = (
            document.input_snapshot.ticker,
            document.investment_case.explanation,
        )
    if source_ticker != ticker:
        raise ValueError("old position reason source ticker differs from its assessment")
    if caa is None and date.fromisoformat(raw["as_of"]) < date.fromisoformat(source["as_of"]):
        raise ValueError("old Position Review predates its assessment source")
    return {
        "kind": "position_reason_source_draft",
        "source_id": identifier,
        "ticker": ticker,
        "source_thesis_id": thesis_id,
        "source_candidate_thesis_id": candidate_id,
        "source_core_sha256": source["core_sha256"],
        "source_case_explanation": explanation,
        "original_investment_reason": explanation if caa is not None else None,
        "original_reason_unavailable_reason": (
            "旧Position Reviewは買付時の評価を保存しておらず、当初理由は未確認"
            if caa is None
            else (
                "旧Thesis3の原文から当初理由を確認する必要がある" if explanation is None else None
            )
        ),
        "source_document": raw,
        "adoption_history": None,
        "new_proposal_required": True,
        "source_validation_limits": limitations,
    }


# Thesis3 is retained as historical material, without a current-equivalence evaluation.


def _legacy3_decimal(value: object) -> Decimal:
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


class _Legacy3ScreeningEstimate(BaseModel):
    """Secondary machine prior retained to explain the Research estimate delta."""

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
        return None if value is None else _legacy3_decimal(value)

    @field_validator("source_ids", mode="before")
    @classmethod
    def _parse_sources(cls, value: object) -> object:
        return _tuple(value)


class _Legacy3ScenarioEstimate(BaseModel):
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
        return _legacy3_decimal(value)


class _Legacy3ScreeningFVBridge(BaseModel):
    """Explain the primary difference from machine prior to Research FV."""

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


class _Legacy3EstimatesNamespace(BaseModel):
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
    scenarios: tuple[_Legacy3ScenarioEstimate, ...]
    screening_fv_bridge: _Legacy3ScreeningFVBridge | None = None

    @field_validator("scenarios", "entry_price_source_ids", "fair_value_source_ids", mode="before")
    @classmethod
    def _parse_scenarios(cls, value: object) -> object:
        return _tuple(value)

    @field_validator("entry_price_basis_yen", "current_fair_value_yen", mode="before")
    @classmethod
    def _parse_entry_price(cls, value: object) -> Decimal:
        return _legacy3_decimal(value)


class _Legacy3EvidenceOverride(BaseModel):
    model_config = _CONFIG

    override_id: Annotated[str, Field(min_length=1)]
    reason: Annotated[str, Field(min_length=1)]
    decision_reference: Annotated[str, Field(min_length=1)]
    thesis_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
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
    def _valid_window(self) -> _Legacy3EvidenceOverride:
        if self.expires_at <= self.approved_at:
            raise ValueError("evidence override expires_at must follow approved_at")
        if (self.expires_at - self.approved_at).total_seconds() > 31 * 86_400:
            raise ValueError("evidence override cannot exceed 31 days")
        return self


class _Legacy3JudgmentNamespace(BaseModel):
    model_config = _CONFIG

    recommendation: Literal["buy", "defer", "reject"]
    proposed_at: datetime
    confidence: Literal["low", "medium", "high"]
    permanent_loss_conclusion: Literal["acceptable", "elevated", "unknown"]
    strongest_countercase: Annotated[str, Field(min_length=1)]
    sizing_action: Literal["normal", "reduced", "none"]

    @field_validator("proposed_at", mode="before")
    @classmethod
    def _parse_time(cls, value: object) -> datetime:
        return _datetime(value)


class _Legacy3ReviewedScenario(BaseModel):
    model_config = _CONFIG

    horizon_years: Literal[3, 5]
    name: Literal["bear", "base", "bull"]
    total_return_cagr_pct: Annotated[float, Field(ge=-100, le=1000)]


class _Legacy3ThesisReview(BaseModel):
    model_config = _CONFIG

    review_id: Annotated[str, Field(min_length=1)]
    reviewer_role: Literal["independent_second_pass"]
    reviewer_identity: Annotated[str, Field(min_length=1)]
    reviewer_run_id: Annotated[str, Field(min_length=1)]
    reviewed_at: datetime
    reviewed_thesis_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    primary_source_check: Literal["verified", "partially_verified", "unverified"]
    checked_source_ids: tuple[Annotated[str, Field(min_length=1)], ...]
    recalculated_scenarios: tuple[_Legacy3ReviewedScenario, ...]
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
    def _change_has_reason(self) -> _Legacy3ThesisReview:
        if self.proposal_changed != bool(self.change_rationale and self.change_rationale.strip()):
            raise ValueError("proposal_changed and change_rationale must be specified together")
        return self


class _Legacy3ThesisDocument(BaseModel):
    """Strict persisted contract with no legacy thesis compatibility fields."""

    model_config = _CONFIG

    schema_version: Literal[3]
    input_snapshot: _Legacy3InputSnapshot
    derived: _LegacyDerivedNamespace
    estimates: _Legacy3EstimatesNamespace
    permanent_loss_risks: tuple[_LegacyPermanentLossRisk, ...]
    judgment: _Legacy3JudgmentNamespace
    independent_review_ref: Annotated[str, Field(min_length=1)] | None = None
    human_evidence_override: _Legacy3EvidenceOverride | None = None

    @field_validator("permanent_loss_risks", mode="before")
    @classmethod
    def _parse_risks(cls, value: object) -> object:
        return _tuple(value)


class _Legacy3InputSnapshot(_LegacyInputSnapshot):
    screening_estimate: _Legacy3ScreeningEstimate | None = None


_Legacy3ThesisDocument.model_rebuild()


def _legacy3_core_hash(document: _Legacy3ThesisDocument) -> str:
    # Draft-only derivation for frozen-contract fixtures, never a published identity.
    payload = document.model_dump(mode="json", exclude={"human_evidence_override"})
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def _legacy3_pair(
    snapshot: Mapping[str, list[dict[str, Any]]], identifier: str
) -> tuple[_Legacy3ThesisDocument, dict[str, Any], dict[str, Any] | None, list[str]]:
    source = next((row for row in snapshot["thesis"] if row["thesis_id"] == identifier), None)
    if source is None:
        raise ValueError(f"missing old assessment source: {identifier}")
    document = _Legacy3ThesisDocument.model_validate_json(str(source["payload"]))
    # Published identity belongs to the saved row, not today's draft model dump.
    identity = source["core_sha256"]
    sources = {item.source_id for item in document.input_snapshot.sources}
    facts = {item.fact_id for item in document.input_snapshot.facts}
    if len(sources) != len(document.input_snapshot.sources) or len(facts) != len(
        document.input_snapshot.facts
    ):
        raise ValueError("old Thesis3 source/fact identities must be unique")
    referenced: tuple[
        _LegacyObservedFact
        | _LegacyDerivedMetric
        | _LegacyPermanentLossRisk
        | _Legacy3ScenarioEstimate,
        ...,
    ] = (
        *document.input_snapshot.facts,
        *document.derived.metrics,
        *document.permanent_loss_risks,
        *document.estimates.scenarios,
    )
    required_sources = set(document.estimates.entry_price_source_ids) | set(
        document.estimates.fair_value_source_ids
    )
    for item in referenced:
        required_sources.update(item.source_ids)
    if document.input_snapshot.screening_estimate is not None:
        required_sources.update(document.input_snapshot.screening_estimate.source_ids)
    required_facts = {document.estimates.market_price_fact_id}
    for metric in document.derived.metrics:
        required_facts.update(metric.input_fact_ids)
    for scenario in document.estimates.scenarios:
        required_facts.update(
            (scenario.starting_earnings_fact_id, scenario.starting_share_count_fact_id)
        )
    if not required_sources <= sources or not required_facts <= facts:
        raise ValueError("old Thesis3 source/fact references are unresolved")
    if (source["ticker"], source["as_of"], source["recommendation"]) != (
        document.input_snapshot.ticker,
        document.input_snapshot.as_of.isoformat(),
        document.judgment.recommendation,
    ):
        raise ValueError("old Thesis3 source header differs")
    limits = ["旧Thesis3の価値見積りは新契約の期間・分配へ自動変換せず、通常Researchで再作成"]
    if source["core_sha256"] is None:
        limits.append("旧記録core hash未保存。構造を確認した材料であり、旧公開identityは未検証")
    elif not isinstance(identity, str) or re.fullmatch(r"[0-9a-f]{64}", identity) is None:
        raise ValueError("old Thesis3 recorded core hash is invalid")
    published = datetime.fromisoformat(str(source["published_at"]))
    if published.utcoffset() is None or document.judgment.proposed_at > published:
        raise ValueError("old Thesis3 source publication time is invalid")
    reviews = [row for row in snapshot["thesis_review"] if row["thesis_id"] == identifier]
    if len(reviews) > 1:
        raise ValueError("old Thesis3 has ambiguous Review references")
    review_row = reviews[0] if reviews else None
    if review_row is None:
        if document.independent_review_ref is not None:
            raise ValueError("old Thesis3 recorded Review reference is missing")
        limits.append("旧独立Review未保存。新Assessmentには今回の独立Reviewが必要")
    else:
        review = _Legacy3ThesisReview.model_validate_json(str(review_row["payload"]))
        review_filename = (
            f"{document.input_snapshot.as_of}-{document.input_snapshot.ticker}-decision-review.yaml"
        )
        if (
            identity is None
            or review.reviewed_thesis_sha256 != identity
            or review.review_id != review_row["review_id"]
            or review.reviewed_at != datetime.fromisoformat(str(review_row["reviewed_at"]))
            or review.reviewed_at < document.judgment.proposed_at
            or (
                document.independent_review_ref is not None
                and document.independent_review_ref
                != review_filename
            )
            or not set(review.checked_source_ids) <= sources
        ):
            raise ValueError("old Thesis3 Review reference/hash differs")
    return document, source, review_row, limits


def assessment3_source_draft(
    snapshot: Mapping[str, list[dict[str, Any]]], identifier: str
) -> dict[str, Any]:
    _document, source, review, limits = _legacy3_pair(snapshot, identifier)
    raw = json.loads(source["payload"])
    snapshot_draft = deepcopy(raw["input_snapshot"])
    snapshot_draft.pop("screening_estimate", None)
    return {
        "kind": "assessment_source_draft",
        "source_schema_version": 3,
        "source_thesis_id": identifier,
        "source_core_sha256": source["core_sha256"],
        "source_published_at": source["published_at"],
        "source_document": raw,
        "source_payload": source["payload"],
        "source_review": None if review is None else json.loads(review["payload"]),
        "source_review_payload": None if review is None else review["payload"],
        "source_validation_limits": limits,
        "assessment_draft": {
            "kind": "stock_assessment",
            "schema_version": 1,
            "input_snapshot": snapshot_draft,
            "derived": raw["derived"],
            "valuation": None,
            "investment_case": None,
            "permanent_loss_risks": raw["permanent_loss_risks"],
            "findings": None,
            "conclusion": None,
            "refresh": None,
        },
        "new_review_required": True,
    }


def verify_alternative_source(
    snapshot: Mapping[str, list[dict[str, Any]]], alternative: Mapping[str, Any]
) -> None:
    identifier = _text(alternative.get("thesis_id"), "old CAA assessment source ID")
    source = next((row for row in snapshot["thesis"] if row["thesis_id"] == identifier), None)
    if source is None:
        raise ValueError(f"missing old CAA assessment source: {identifier}")
    if json.loads(source["payload"]).get("schema_version") == 3:
        old, source, review, _limits = _legacy3_pair(snapshot, identifier)
        ticker = old.input_snapshot.ticker
        review_id = None if review is None else review["review_id"]
    else:
        current, source, review = legacy_assessment_pair(snapshot, identifier)
        ticker, review_id = current.input_snapshot.ticker, review["review_id"]
    if (ticker, source["core_sha256"], review_id) != (
        alternative.get("ticker"),
        alternative.get("thesis_core_sha256"),
        alternative.get("thesis_review_id"),
    ):
        raise ValueError("old CAA alternative differs from its exact assessment/Review source")
