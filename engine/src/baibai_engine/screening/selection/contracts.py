"""Current contract for the single ranked research-review set."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from hashlib import sha256
from math import isfinite

from pydantic import BaseModel, ConfigDict, Field, field_validator

from baibai_engine.appdb.json import canonical_json
from baibai_engine.foundation.coerce import optional_float
from baibai_engine.screening.rule_config import CandidateDiagnosticRules, SelectionLiquidityRules

from .candidate_diagnostics import _candidate_diagnostics
from .ranking import (
    _best_selection_evidence,
    _evidence_pattern_order_rank,
    _sizing_eligible_evidence_hits,
)
from .records import candidate_record_from_mapping
from .summaries import (
    _candidate_reason_tags,
    _candidate_risk_tags,
    _decision_input_seed,
    _ranked_set_machine_projection,
)


class SelectionMethodParameters(BaseModel):
    """Runtime values that can change membership or order."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    review_cap: int = Field(ge=0)
    expected_return_model_id: str = Field(min_length=1)
    screening_rules_hash: str | None
    required_jpx_flags: tuple[str, ...]
    liquidity_parameters: SelectionLiquidityRules
    candidate_diagnostic_parameters: CandidateDiagnosticRules
    evidence_pattern_order: tuple[str, ...]

    @field_validator("required_jpx_flags", "evidence_pattern_order", mode="before")
    @classmethod
    def _tuples(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, list | tuple) or not all(isinstance(item, str) for item in value):
            raise ValueError("must be an array of strings")
        return tuple(value)


class SelectionContractError(ValueError):
    """Raised when a selection is not the current ranked-set contract."""


def selection_method_hash(parameters: SelectionMethodParameters) -> str:
    representation = {
        "expected_return_model_id": parameters.expected_return_model_id,
        "screening_rules_hash": parameters.screening_rules_hash,
        "required_jpx_flags": list(parameters.required_jpx_flags),
        "liquidity_parameters": parameters.liquidity_parameters.model_dump(mode="json"),
        "candidate_diagnostic_parameters": (
            parameters.candidate_diagnostic_parameters.model_dump(mode="json")
        ),
        "ordering": [
            "er_annual_desc",
            "primary_evidence_pattern_order",
            "evidence_strength",
            "ticker_asc",
        ],
        "evidence_pattern_order": list(parameters.evidence_pattern_order),
        "review_cap": parameters.review_cap,
    }
    return sha256(canonical_json(representation).encode()).hexdigest()


def validate_selection_payload(payload: Mapping[str, object]) -> SelectionMethodParameters:
    try:
        parameters = SelectionMethodParameters.model_validate(
            _required_mapping(payload, "method_parameters")
        )
    except ValueError as error:
        raise SelectionContractError("invalid selection method parameters") from error
    if tuple(sorted(set(parameters.required_jpx_flags))) != parameters.required_jpx_flags:
        raise SelectionContractError("required JPX flags must be unique and sorted")
    if payload.get("method_hash") != selection_method_hash(parameters):
        raise SelectionContractError("selection method hash does not match its inputs")
    selection = _required_mapping(payload, "selection")
    if selection.get("screening_rules_hash") != parameters.screening_rules_hash:
        raise SelectionContractError("selection rules hash does not match method inputs")
    if selection.get("er_model_version") != parameters.expected_return_model_id:
        raise SelectionContractError("selection model does not match method inputs")
    review_basis = _required_mapping(payload, "review_basis")
    if set(review_basis) != {"judged_through_shortlist_id"}:
        raise SelectionContractError("review basis has an invalid shape")
    reviewed_through = review_basis.get("judged_through_shortlist_id")
    if reviewed_through is not None and (
        not isinstance(reviewed_through, str) or not reviewed_through.strip()
    ):
        raise SelectionContractError("review basis shortlist ID is invalid")

    rows = _ranked_set(payload)
    if len(rows) > parameters.review_cap:
        raise SelectionContractError("ranked set exceeds review cap")
    for rank, row in enumerate(rows, start=1):
        if row.get("rank") != rank:
            raise SelectionContractError("ranked set ranks must be contiguous")
        value = row.get("er_annual")
        if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
            raise SelectionContractError("ranked set E[r] must be finite")
        displayed = row.get("expected_return_pct")
        if (
            isinstance(displayed, bool)
            or not isinstance(displayed, int | float)
            or float(displayed) != round(float(value) * 100, 4)
        ):
            raise SelectionContractError("displayed E[r] does not match its native value")
    return parameters


def expected_ranked_set(
    candidates: Sequence[Mapping[str, object]],
    *,
    parameters: SelectionMethodParameters,
    asof_date: str,
) -> tuple[tuple[str, float, str | None, Mapping[str, object]], ...]:
    """Recompute membership, order, and machine projection from the source run."""
    required_jpx_flags = frozenset(parameters.required_jpx_flags)
    asof = date.fromisoformat(asof_date)
    ranked: list[
        tuple[tuple[object, ...], tuple[str, float, str | None, Mapping[str, object]]]
    ] = []
    for raw in candidates:
        candidate = candidate_record_from_mapping(raw)
        if any(
            value is None
            for value in (
                candidate.market_cap_oku,
                candidate.avg_turnover_oku,
                candidate.listing_span_days,
                candidate.jpx_flags,
            )
        ) or not parameters.liquidity_parameters.matches(
            market_cap_oku=candidate.market_cap_oku,
            avg_turnover_oku=candidate.avg_turnover_oku,
            listing_span_days=candidate.listing_span_days,
            jpx_flags=candidate.jpx_flags,
            required_jpx_flags=required_jpx_flags,
            require_facts=True,
        ):
            continue
        er_annual = optional_float(candidate.metrics.get("er_annual"))
        if er_annual is None:
            continue
        primary_pattern, _, strength_key = _best_selection_evidence(
            _sizing_eligible_evidence_hits(candidate.evidence_hits),
            evidence_pattern_order=parameters.evidence_pattern_order,
        )
        with_seed = {
            **raw,
            "candidate_diagnostics": _candidate_diagnostics(
                candidate, parameters.candidate_diagnostic_parameters
            ),
            "primary_evidence_pattern_id": primary_pattern,
            "deterioration_gate_unmeasurable": (
                candidate.metrics.get("deterioration_gate_unmeasurable") is True
            ),
            "decision_input_seed": _decision_input_seed(raw, asof_date=asof),
        }
        with_seed["reason_tags"] = _candidate_reason_tags(with_seed)
        with_seed["risk_tags"] = _candidate_risk_tags(with_seed)
        ranked.append(
            (
                (
                    -er_annual,
                    _evidence_pattern_order_rank(
                        primary_pattern, parameters.evidence_pattern_order
                    ),
                    *strength_key,
                    candidate.ticker,
                ),
                (
                    candidate.ticker,
                    er_annual,
                    primary_pattern,
                    _ranked_set_machine_projection(with_seed),
                ),
            )
        )
    ranked.sort(key=lambda item: item[0])
    return tuple(row for _, row in ranked[: parameters.review_cap])


def _ranked_set(payload: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    value = payload.get("ranked_set")
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise SelectionContractError("ranked_set must be an array")
    if not all(isinstance(row, Mapping) for row in value):
        raise SelectionContractError("ranked_set contains an invalid row")
    tickers = [row.get("ticker") for row in value]
    if not all(isinstance(ticker, str) and ticker for ticker in tickers):
        raise SelectionContractError("ranked_set contains an invalid ticker")
    if len(tickers) != len(set(tickers)):
        raise SelectionContractError("ranked_set contains duplicate tickers")
    return tuple(value)


def _required_mapping(payload: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise SelectionContractError(f"{key} must be an object")
    return value


__all__ = [
    "SelectionContractError",
    "SelectionMethodParameters",
    "expected_ranked_set",
    "selection_method_hash",
    "validate_selection_payload",
]
