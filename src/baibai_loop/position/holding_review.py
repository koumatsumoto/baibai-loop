"""Holding review contract: thesis health and after-tax replacement drive action.

A holding review draft decides ``hold / add / reduce / exit`` for one open
position from *thesis health* (invalidation, permanent-loss axes, evidence
freshness, forward 5y estimate) and an *after-tax replacement comparison*, not
from price moves or a mechanical fair-value threshold. Reaching fair value is a
review trigger, not an automatic sell; a broken thesis is the priority sell
candidate; a price decline on its own is never a reason to exit.

The draft carries the distilled upstream values (permanent-loss axes and forward
CAGRs come from the decision packet, the exit-tax basis from the portfolio
ledger), so this module stays self-contained and reuses the ledger's confirmed /
estimated tax split rather than modelling any account tax engine.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from baibai_loop.foundation.yaml_io import safe_load
from baibai_loop.position.ledger import estimated_exit_tax_for_gain_yen

_CONFIG = ConfigDict(frozen=True, strict=True, extra="forbid", allow_inf_nan=False)
_TICKER = r"^[0-9A-Z]{4}$"
_HORIZON_YEARS = 5

# Canonical permanent-loss axes; identical set to the decision packet. A review
# that does not cover every axis exactly once is incomplete (parent D2).
_RISK_AXES: tuple[str, ...] = (
    "funding_liquidity",
    "debt_repayment",
    "cash_flow",
    "dilution",
    "customer_concentration",
    "structural_decline",
    "governance_accounting",
)

# Primary evidence older than this is stale enough to flag; same window the
# decision packet uses for permanent-loss evidence.
_EVIDENCE_STALE_DAYS = 400

type Action = Literal["hold", "add", "reduce", "exit"]
type PermanentLossConclusion = Literal["acceptable", "elevated", "unknown"]


class HoldingReviewError(ValueError):
    """Raised when a holding review draft cannot be loaded."""


def _as_date(value: object) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise ValueError(f"expected an ISO date, got {value!r}")


def _as_tuple(value: object) -> object:
    # Strict mode wants a tuple; YAML sequences load as lists. Convert here so
    # callers can write ordinary YAML lists.
    return tuple(value) if isinstance(value, list) else value


class PermanentLossAxis(BaseModel):
    model_config = _CONFIG

    axis: Literal[
        "funding_liquidity",
        "debt_repayment",
        "cash_flow",
        "dilution",
        "customer_concentration",
        "structural_decline",
        "governance_accounting",
    ]
    assessment: Literal["acceptable", "adverse", "unknown"]
    evidence_status: Literal["verified", "partially_verified", "unverified"]


class EvidenceFreshness(BaseModel):
    model_config = _CONFIG

    latest_source_as_of: date
    age_days: Annotated[int, Field(ge=0)]

    @field_validator("latest_source_as_of", mode="before")
    @classmethod
    def _parse_date(cls, value: object) -> date:
        return _as_date(value)


class Current5yEstimate(BaseModel):
    model_config = _CONFIG

    status: Literal["resolved", "unresolved"]
    forward_5y_cagr_pct: Annotated[float, Field(gt=-100, le=1000)] | None = None

    @model_validator(mode="after")
    def _require_value_exactly_when_resolved(self) -> Current5yEstimate:
        if (self.status == "resolved") != (self.forward_5y_cagr_pct is not None):
            raise ValueError(
                "current_5y_estimate requires forward_5y_cagr_pct exactly when status=resolved"
            )
        return self


class ThesisHealth(BaseModel):
    model_config = _CONFIG

    invalidation_status: Literal["intact", "at_risk", "broken"]
    permanent_loss_axes: Annotated[tuple[PermanentLossAxis, ...], Field(min_length=7, max_length=7)]
    evidence_freshness: EvidenceFreshness
    current_5y_estimate: Current5yEstimate

    @field_validator("permanent_loss_axes", mode="before")
    @classmethod
    def _parse_axes(cls, value: object) -> object:
        return _as_tuple(value)


class HoldLeg(BaseModel):
    model_config = _CONFIG

    market_value_yen: Annotated[int, Field(gt=0)]
    deployed_cost_yen: Annotated[int, Field(ge=0)]
    forward_5y_cagr_pct: Annotated[float, Field(gt=-100, le=1000)]


class CandidateLeg(BaseModel):
    model_config = _CONFIG

    ticker: Annotated[str, Field(pattern=_TICKER)]
    forward_5y_cagr_pct: Annotated[float, Field(gt=-100, le=1000)]


class ExitTax(BaseModel):
    model_config = _CONFIG

    # Confirmed reuses the ledger ``tax_confirmed`` amount; estimated reuses the
    # ledger effective-rate basis (FIFO gross unrealized gain * rate); unknown
    # keeps the comparison from asserting a single verdict (NISA / loss offset).
    tax_basis: Literal["confirmed", "estimated", "unknown"]
    rate_bps: Annotated[int, Field(ge=0, le=10_000)] | None = None
    tax_yen: Annotated[int, Field(ge=0)] | None = None
    estimated_exit_tax_basis: Literal["ledger_fifo_gross_unrealized_gain"] | None = None

    @model_validator(mode="after")
    def _require_tax_basis_inputs(self) -> ExitTax:
        match self.tax_basis:
            case "confirmed":
                if (
                    self.tax_yen is None
                    or self.rate_bps is not None
                    or self.estimated_exit_tax_basis is not None
                ):
                    raise ValueError(
                        "exit_tax.tax_basis=confirmed requires tax_yen and forbids "
                        "rate_bps and estimated_exit_tax_basis"
                    )
            case "estimated":
                if (
                    self.rate_bps is None
                    or self.estimated_exit_tax_basis is None
                    or self.tax_yen is not None
                ):
                    raise ValueError(
                        "exit_tax.tax_basis=estimated requires rate_bps and "
                        "estimated_exit_tax_basis and forbids tax_yen"
                    )
            case "unknown":
                if any(
                    value is not None
                    for value in (self.rate_bps, self.tax_yen, self.estimated_exit_tax_basis)
                ):
                    raise ValueError(
                        "exit_tax.tax_basis=unknown forbids tax_yen, rate_bps, "
                        "and estimated_exit_tax_basis"
                    )
        return self


class ReplacementComparison(BaseModel):
    model_config = _CONFIG

    status: Literal["no_candidate", "evaluated"]
    hold: HoldLeg | None = None
    candidate: CandidateLeg | None = None
    exit_tax: ExitTax | None = None


class AddContext(BaseModel):
    model_config = _CONFIG

    current_price_yen: Annotated[int, Field(gt=0)]
    max_acceptable_price_yen: Annotated[int, Field(gt=0)]
    available_cash_yen: Annotated[int, Field(ge=0)]
    concentration_ok: bool


class ValuationReview(BaseModel):
    model_config = _CONFIG

    status: Literal["resolved", "unresolved"]
    current_price_yen: Annotated[int, Field(gt=0)] | None = None
    fair_value_yen: Annotated[int, Field(gt=0)] | None = None
    review_trigger: bool | None = None

    @model_validator(mode="after")
    def _require_fair_value_inputs_exactly_when_resolved(self) -> ValuationReview:
        values = (self.current_price_yen, self.fair_value_yen, self.review_trigger)
        if self.status == "unresolved":
            if any(value is not None for value in values):
                raise ValueError(
                    "valuation_review.status=unresolved forbids price, fair value, "
                    "and review_trigger"
                )
            return self
        if any(value is None for value in values):
            raise ValueError(
                "valuation_review.status=resolved requires price, fair value, and review_trigger"
            )
        assert self.current_price_yen is not None
        assert self.fair_value_yen is not None
        assert self.review_trigger is not None
        if self.review_trigger != (self.current_price_yen >= self.fair_value_yen):
            raise ValueError(
                "valuation_review.review_trigger must equal current_price_yen >= fair_value_yen"
            )
        return self


class ReduceContext(BaseModel):
    model_config = _CONFIG

    concentration_exceeded: bool


class HoldingReviewDocument(BaseModel):
    model_config = _CONFIG

    schema_version: Literal[1]
    as_of: date
    position_id: Annotated[str, Field(min_length=1)]
    ticker: Annotated[str, Field(pattern=_TICKER)]
    thesis_health: ThesisHealth
    valuation_review: ValuationReview
    replacement_comparison: ReplacementComparison
    action: Literal["hold", "add", "reduce", "exit"]
    add_context: AddContext | None = None
    reduce_context: ReduceContext | None = None
    note: Annotated[str, Field(min_length=1)] | None = None

    @field_validator("as_of", mode="before")
    @classmethod
    def _parse_date(cls, value: object) -> date:
        return _as_date(value)

    @model_validator(mode="after")
    def _validate_replacement_inputs(self) -> HoldingReviewDocument:
        replacement = self.replacement_comparison
        estimate = self.thesis_health.current_5y_estimate
        if estimate.status == "unresolved" and self.add_context is not None:
            raise ValueError("add_context requires a resolved current_5y_estimate")
        if self.add_context is not None:
            valuation = self.valuation_review
            if valuation.status != "resolved" or valuation.current_price_yen is None:
                raise ValueError("add_context requires a resolved valuation_review")
            if self.add_context.current_price_yen != valuation.current_price_yen:
                raise ValueError(
                    "add_context.current_price_yen must match valuation_review.current_price_yen"
                )
        if replacement.status != "evaluated":
            return self
        if (
            replacement.hold is None
            or replacement.candidate is None
            or replacement.exit_tax is None
        ):
            return self
        if estimate.status != "resolved" or estimate.forward_5y_cagr_pct is None:
            raise ValueError(
                "replacement_comparison.status=evaluated requires a resolved current_5y_estimate"
            )
        if replacement.hold.forward_5y_cagr_pct != estimate.forward_5y_cagr_pct:
            raise ValueError(
                "replacement_comparison.hold.forward_5y_cagr_pct must match current_5y_estimate"
            )
        if replacement.candidate.ticker == self.ticker:
            raise ValueError("replacement candidate ticker must differ from the holding ticker")
        return self


@dataclass(frozen=True, slots=True)
class HoldingReviewResult:
    review_status: Literal["incomplete", "complete"]
    computed_action: Action
    permanent_loss_conclusion: PermanentLossConclusion
    replacement_edge_yen: int | None
    tax_basis: Literal["confirmed", "estimated", "unknown"] | None
    breakeven_exit_tax_rate_bps: int | None
    replacement_edge_at_zero_tax_yen: int | None
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


def holding_review_json_schema() -> dict[str, object]:
    schema = HoldingReviewDocument.model_json_schema()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "holding-review",
        **schema,
    }


def load_holding_review(path: Path) -> HoldingReviewDocument:
    try:
        raw = safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise HoldingReviewError(f"failed to load holding review: {error}") from error
    if not isinstance(raw, Mapping):
        raise HoldingReviewError("holding review root must be a mapping")
    try:
        return HoldingReviewDocument.model_validate(raw)
    except ValidationError as error:
        raise HoldingReviewError(str(error)) from error


def evaluate_holding_review(document: HoldingReviewDocument) -> HoldingReviewResult:
    """Recompute action and drift flags from thesis health and replacement.

    The recorded ``action`` must equal the computed one; a mismatch is an error so
    a persisted draft can never disagree with its own inputs.
    """

    errors: list[str] = []
    warnings: list[str] = []

    health = document.thesis_health
    _validate_evidence_freshness(document, errors)
    axes_complete = _axes_complete(health.permanent_loss_axes, errors)
    conclusion = _permanent_loss_conclusion(health.permanent_loss_axes)

    if health.evidence_freshness.age_days > _EVIDENCE_STALE_DAYS:
        warnings.append(
            f"primary evidence is {health.evidence_freshness.age_days} days old "
            f"(> {_EVIDENCE_STALE_DAYS}); refresh before acting on the estimate"
        )
    if health.current_5y_estimate.status == "unresolved":
        warnings.append(
            "current_5y_estimate is unresolved; forward hold value cannot be "
            "compared until the decision packet is available"
        )

    edge, breakeven, edge_zero, tax_basis = _replacement_edge(
        document.replacement_comparison, errors, warnings
    )

    computed = _decide_action(
        invalidation_status=health.invalidation_status,
        conclusion=conclusion,
        current_5y_estimate_resolved=health.current_5y_estimate.status == "resolved",
        replacement_edge_yen=edge,
        add_context=document.add_context,
        reduce_context=document.reduce_context,
    )
    if computed != document.action:
        errors.append(f"recorded action {document.action!r} disagrees with computed {computed!r}")

    review_status: Literal["incomplete", "complete"] = "complete" if axes_complete else "incomplete"
    return HoldingReviewResult(
        review_status=review_status,
        computed_action=computed,
        permanent_loss_conclusion=conclusion,
        replacement_edge_yen=edge,
        tax_basis=tax_basis,
        breakeven_exit_tax_rate_bps=breakeven,
        replacement_edge_at_zero_tax_yen=edge_zero,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def _axes_complete(axes: tuple[PermanentLossAxis, ...], errors: list[str]) -> bool:
    seen = [axis.axis for axis in axes]
    if set(seen) != set(_RISK_AXES) or len(seen) != len(set(seen)):
        errors.append(
            f"permanent_loss_axes must cover the 7 canonical axes exactly once; got {sorted(seen)}"
        )
        return False
    return True


def _permanent_loss_conclusion(
    axes: tuple[PermanentLossAxis, ...],
) -> PermanentLossConclusion:
    # An exit needs a fully verified adverse axis. A partially verified adverse
    # signal remains unknown so it can trigger further review without asserting
    # the tax-bearing all-position exit reserved for thesis break.
    if any(axis.assessment == "adverse" and axis.evidence_status == "verified" for axis in axes):
        return "elevated"
    if any(axis.assessment in {"adverse", "unknown"} for axis in axes):
        return "unknown"
    return "acceptable"


def _replacement_edge(
    replacement: ReplacementComparison,
    errors: list[str],
    warnings: list[str],
) -> tuple[int | None, int | None, int | None, Literal["confirmed", "estimated", "unknown"] | None]:
    if replacement.status == "no_candidate":
        for name, present in (
            ("hold", replacement.hold),
            ("candidate", replacement.candidate),
            ("exit_tax", replacement.exit_tax),
        ):
            if present is not None:
                errors.append(f"replacement_comparison.status=no_candidate must omit {name}")
        return None, None, None, None

    hold = replacement.hold
    candidate = replacement.candidate
    exit_tax = replacement.exit_tax
    if hold is None or candidate is None or exit_tax is None:
        errors.append(
            "replacement_comparison.status=evaluated requires hold, candidate, and exit_tax"
        )
        return None, None, None, None

    hold_terminal = hold.market_value_yen * (1 + hold.forward_5y_cagr_pct / 100) ** _HORIZON_YEARS
    switch_zero = (
        hold.market_value_yen * (1 + candidate.forward_5y_cagr_pct / 100) ** _HORIZON_YEARS
    )
    edge_zero = round(switch_zero - hold_terminal)

    gain = max(0, hold.market_value_yen - hold.deployed_cost_yen)
    breakeven = _breakeven_exit_tax_bps(
        market_value_yen=hold.market_value_yen,
        gain_yen=gain,
        hold_terminal=hold_terminal,
        candidate_cagr_pct=candidate.forward_5y_cagr_pct,
    )

    exit_tax_yen = _exit_tax_yen(exit_tax, gain_yen=gain, errors=errors)
    if exit_tax_yen is None:
        # Tax unknown: do not assert a single verdict; surface the sensitivity
        # (breakeven rate and zero-tax edge) and let the action fall through.
        warnings.append(
            "exit tax is unknown; replacement edge is shown as a sensitivity "
            "(breakeven rate and zero-tax edge), not a switch verdict"
        )
        return None, breakeven, edge_zero, exit_tax.tax_basis

    redeployable = hold.market_value_yen - exit_tax_yen
    switch_terminal = redeployable * (1 + candidate.forward_5y_cagr_pct / 100) ** _HORIZON_YEARS
    edge = round(switch_terminal - hold_terminal)
    return edge, breakeven, edge_zero, exit_tax.tax_basis


def _exit_tax_yen(exit_tax: ExitTax, *, gain_yen: int, errors: list[str]) -> int | None:
    match exit_tax.tax_basis:
        case "confirmed":
            if exit_tax.tax_yen is None:
                errors.append("exit_tax.tax_basis=confirmed requires tax_yen")
                return None
            return exit_tax.tax_yen
        case "estimated":
            assert exit_tax.rate_bps is not None
            return estimated_exit_tax_for_gain_yen(
                rate_bps=exit_tax.rate_bps,
                basis=exit_tax.estimated_exit_tax_basis,
                gross_unrealized_gain_yen=gain_yen,
            )
        case "unknown":
            return None


def _breakeven_exit_tax_bps(
    *,
    market_value_yen: int,
    gain_yen: int,
    hold_terminal: float,
    candidate_cagr_pct: float,
) -> int | None:
    # Rate at which switch_terminal(rate) == hold_terminal. Only positive rates
    # are meaningful: if the candidate does not beat holding even at zero tax,
    # there is no breakeven and switching never wins.
    if gain_yen <= 0:
        return None
    redeploy_needed = hold_terminal / (1 + candidate_cagr_pct / 100) ** _HORIZON_YEARS
    rate = (market_value_yen - redeploy_needed) / gain_yen * 10_000
    if rate <= 0:
        return None
    return round(rate)


def _decide_action(
    *,
    invalidation_status: Literal["intact", "at_risk", "broken"],
    conclusion: PermanentLossConclusion,
    current_5y_estimate_resolved: bool,
    replacement_edge_yen: int | None,
    add_context: AddContext | None,
    reduce_context: ReduceContext | None,
) -> Action:
    # Priority 1: a broken thesis or a verified permanent-loss escalation is the
    # priority sell candidate. Price moves never reach this branch.
    if invalidation_status == "broken" or conclusion == "elevated":
        return "exit"
    # Priority 2: a candidate that beats holding after realized exit tax justifies
    # trimming. Reduce (not full exit) keeps a healthy thesis while freeing
    # capital; unknown tax leaves edge None and cannot trigger this.
    if (
        invalidation_status == "at_risk"
        or (reduce_context is not None and reduce_context.concentration_exceeded)
        or (replacement_edge_yen is not None and replacement_edge_yen > 0)
    ):
        return "reduce"
    # Priority 3: an intact thesis trading below its max acceptable price with
    # cash and headroom is a pull-back add.
    if (
        conclusion == "acceptable"
        and current_5y_estimate_resolved
        and add_context is not None
        and add_context.current_price_yen < add_context.max_acceptable_price_yen
        and add_context.available_cash_yen > 0
        and add_context.concentration_ok
    ):
        return "add"
    # Default: hold. Reaching fair value is a review trigger, not an auto-sell.
    return "hold"


def _validate_evidence_freshness(document: HoldingReviewDocument, errors: list[str]) -> None:
    freshness = document.thesis_health.evidence_freshness
    if freshness.latest_source_as_of > document.as_of:
        errors.append("evidence_freshness.latest_source_as_of cannot be after review as_of")
        return
    expected_age_days = (document.as_of - freshness.latest_source_as_of).days
    if freshness.age_days != expected_age_days:
        errors.append(
            "evidence_freshness.age_days must equal the elapsed days from "
            "latest_source_as_of to review as_of"
        )


def result_to_payload(
    document: HoldingReviewDocument, result: HoldingReviewResult
) -> dict[str, object]:
    return {
        "as_of": document.as_of.isoformat(),
        "position_id": document.position_id,
        "ticker": document.ticker,
        "review_status": result.review_status,
        "recorded_action": document.action,
        "computed_action": result.computed_action,
        "permanent_loss_conclusion": result.permanent_loss_conclusion,
        "replacement": {
            "status": document.replacement_comparison.status,
            "edge_yen": result.replacement_edge_yen,
            "tax_basis": result.tax_basis,
            "breakeven_exit_tax_rate_bps": result.breakeven_exit_tax_rate_bps,
            "edge_at_zero_tax_yen": result.replacement_edge_at_zero_tax_yen,
        },
        "valuation_review": {
            "status": document.valuation_review.status,
            "review_trigger": document.valuation_review.review_trigger,
        },
        "note": (
            "hold/add/reduce/exit is a review draft, not an automatic exit; "
            "fair value is a review trigger and price decline alone is never an exit reason"
        ),
    }
