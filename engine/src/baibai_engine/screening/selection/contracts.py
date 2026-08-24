"""Explicit Opportunity Discovery identities for the adopted Value / Carry wire contract.

This module deliberately is not a registry or a policy DSL.  It gives the two
implemented policies one canonical representation so a stored selection can say
exactly which executable contract produced it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date
from hashlib import sha256
from math import isfinite

from pydantic import BaseModel, ConfigDict, Field, field_validator

from baibai_engine.appdb.json import canonical_json
from baibai_engine.foundation.coerce import optional_float
from baibai_engine.screening.rule_config import (
    CandidateDiagnosticRules,
    SelectionLiquidityRules,
)

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
    _longlist_machine_projection,
)

VALUE_CARRY_OPPORTUNITY_LANE_ID = "value-carry"
VALUE_CARRY_SELECTION_POLICY_ID = "value-carry-v1"
VALUE_CARRY_ONLY_ATTENTION_POLICY_ID = "value-carry-only-v1"
RESEARCH_GATE_CONTRACT_ID = "research-gate-v1"

COMMON_INVESTABLE_GATE_ID = "required-jpx-flags-and-liquidity-v1"


class ValueCarryOnlyAttentionParameters(BaseModel):
    """Runtime parameters accepted by the Core-only Attention Policy."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    value_carry_limit: int = Field(ge=0, le=100)


class ValueCarrySelectionPolicyParameters(BaseModel):
    """Exact runtime inputs hashed as the Value / Carry policy identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    lane_longlist_depth: int = Field(ge=0, le=100)
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


class ValueCarrySelectionContractError(ValueError):
    """Raised when a new selection does not carry the adopted exact contract."""


def value_carry_selection_policy_representation(
    *,
    lane_longlist_depth: int,
    expected_return_model_id: str,
    screening_rules_hash: str | None,
    required_jpx_flags: Sequence[str],
    liquidity_parameters: Mapping[str, object],
    candidate_diagnostic_parameters: Mapping[str, object],
    evidence_pattern_order: Sequence[str],
) -> dict[str, object]:
    """Return the exact executable Value / Carry v1 contract."""

    return {
        "opportunity_lane_id": VALUE_CARRY_OPPORTUNITY_LANE_ID,
        "selection_policy_id": VALUE_CARRY_SELECTION_POLICY_ID,
        "expected_return_model_id": expected_return_model_id,
        "screening_rules_hash": screening_rules_hash,
        "common_investable_gate": {
            "common_investable_gate_id": COMMON_INVESTABLE_GATE_ID,
            "required_jpx_flags": sorted(required_jpx_flags),
            "liquidity_parameters": dict(liquidity_parameters),
        },
        "candidate_diagnostic_parameters": dict(candidate_diagnostic_parameters),
        "eligibility": {"er_annual": "finite"},
        "ordering": [
            "er_annual_desc",
            "primary_evidence_pattern_order",
            "evidence_strength",
            "ticker_asc",
        ],
        "evidence_pattern_order": list(evidence_pattern_order),
        "lane_longlist_depth": lane_longlist_depth,
    }


def value_carry_selection_policy_hash(
    *,
    lane_longlist_depth: int,
    expected_return_model_id: str,
    screening_rules_hash: str | None,
    required_jpx_flags: Sequence[str],
    liquidity_parameters: Mapping[str, object],
    candidate_diagnostic_parameters: Mapping[str, object],
    evidence_pattern_order: Sequence[str],
) -> str:
    return _canonical_sha256(
        value_carry_selection_policy_representation(
            lane_longlist_depth=lane_longlist_depth,
            expected_return_model_id=expected_return_model_id,
            screening_rules_hash=screening_rules_hash,
            required_jpx_flags=required_jpx_flags,
            liquidity_parameters=liquidity_parameters,
            candidate_diagnostic_parameters=candidate_diagnostic_parameters,
            evidence_pattern_order=evidence_pattern_order,
        )
    )


def validate_value_carry_selection_payload(
    payload: Mapping[str, object],
) -> ValueCarrySelectionPolicyParameters:
    """Fail closed on the exact Value / Carry selection and Review Set contract.

    The run store is the new-write boundary.  Shortlist publication must not be able
    to turn malformed or internally inconsistent row provenance into an ``exact``
    canonical judgment merely because the draft repeats those values.
    """

    origin = _required_mapping(payload, "longlist_origin")
    if set(origin) != {
        "opportunity_lane_id",
        "selection_policy_id",
        "selection_policy_hash",
    }:
        raise ValueCarrySelectionContractError("longlist_origin has an invalid shape")
    if origin.get("opportunity_lane_id") != VALUE_CARRY_OPPORTUNITY_LANE_ID:
        raise ValueCarrySelectionContractError("unknown Opportunity Lane ID")
    if origin.get("selection_policy_id") != VALUE_CARRY_SELECTION_POLICY_ID:
        raise ValueCarrySelectionContractError("unknown Selection Policy ID")
    selection_policy_hash = _required_sha256(origin, "selection_policy_hash")
    try:
        policy_parameters = ValueCarrySelectionPolicyParameters.model_validate(
            _required_mapping(payload, "selection_policy_parameters")
        )
    except ValueError as error:
        raise ValueCarrySelectionContractError("invalid Selection Policy parameters") from error
    if tuple(sorted(set(policy_parameters.required_jpx_flags))) != (
        policy_parameters.required_jpx_flags
    ):
        raise ValueCarrySelectionContractError(
            "Selection Policy JPX flags must be unique and sorted"
        )
    expected_selection_hash = value_carry_selection_policy_hash(
        lane_longlist_depth=policy_parameters.lane_longlist_depth,
        expected_return_model_id=policy_parameters.expected_return_model_id,
        screening_rules_hash=policy_parameters.screening_rules_hash,
        required_jpx_flags=policy_parameters.required_jpx_flags,
        liquidity_parameters=policy_parameters.liquidity_parameters.model_dump(mode="json"),
        candidate_diagnostic_parameters=policy_parameters.candidate_diagnostic_parameters.model_dump(
            mode="json"
        ),
        evidence_pattern_order=policy_parameters.evidence_pattern_order,
    )
    if selection_policy_hash != expected_selection_hash:
        raise ValueCarrySelectionContractError("Selection Policy hash does not match its inputs")
    selection_metadata = _required_mapping(payload, "selection")
    if selection_metadata.get("screening_rules_hash") != policy_parameters.screening_rules_hash:
        raise ValueCarrySelectionContractError(
            "Selection Policy rules hash does not match selection metadata"
        )
    if selection_metadata.get("er_model_version") != policy_parameters.expected_return_model_id:
        raise ValueCarrySelectionContractError(
            "Selection Policy Model ID does not match selection metadata"
        )

    if payload.get("attention_policy_id") != VALUE_CARRY_ONLY_ATTENTION_POLICY_ID:
        raise ValueCarrySelectionContractError("unknown Attention Policy ID")
    attention_policy_hash = _required_sha256(payload, "attention_policy_hash")
    try:
        parameters = ValueCarryOnlyAttentionParameters.model_validate(
            _required_mapping(payload, "attention_policy_parameters")
        )
    except ValueError as error:
        raise ValueCarrySelectionContractError("invalid Attention Policy parameters") from error
    expected_attention_hash = value_carry_only_attention_policy_hash(
        selection_policy_hash=selection_policy_hash,
        parameters=parameters,
    )
    if attention_policy_hash != expected_attention_hash:
        raise ValueCarrySelectionContractError("Attention Policy hash does not match its inputs")
    if parameters.value_carry_limit != policy_parameters.lane_longlist_depth:
        raise ValueCarrySelectionContractError(
            "Attention limit must match the Selection Policy longlist depth"
        )

    review_basis = _required_mapping(payload, "review_basis")
    if set(review_basis) != {"judged_through_shortlist_id"}:
        raise ValueCarrySelectionContractError("Review Basis has an invalid shape")
    reviewed_through = review_basis.get("judged_through_shortlist_id")
    if reviewed_through is not None and (
        not isinstance(reviewed_through, str) or not reviewed_through.strip()
    ):
        raise ValueCarrySelectionContractError("Review Basis shortlist ID is invalid")

    review_tickers = _required_tickers(payload.get("review_tickers"), label="Review Set")
    raw_longlist = payload.get("longlist")
    if raw_longlist is None:
        longlist: tuple[Mapping[str, object], ...] = ()
    elif isinstance(raw_longlist, Sequence) and not isinstance(raw_longlist, str | bytes):
        if not all(isinstance(row, Mapping) for row in raw_longlist):
            raise ValueCarrySelectionContractError("longlist contains an invalid row")
        longlist = tuple(raw_longlist)
    else:
        raise ValueCarrySelectionContractError("longlist must be an array")
    longlist_tickers = _required_tickers([row.get("ticker") for row in longlist], label="longlist")
    if review_tickers != longlist_tickers:
        raise ValueCarrySelectionContractError("Review Set must equal longlist in order")
    if len(longlist) > parameters.value_carry_limit:
        raise ValueCarrySelectionContractError("longlist exceeds the Attention Policy limit")

    for rank, row in enumerate(longlist, start=1):
        if row.get("opportunity_lane_id") != VALUE_CARRY_OPPORTUNITY_LANE_ID:
            raise ValueCarrySelectionContractError("longlist row has an invalid Lane ID")
        if row.get("selection_policy_id") != VALUE_CARRY_SELECTION_POLICY_ID:
            raise ValueCarrySelectionContractError("longlist row has an invalid Policy ID")
        if row.get("selection_policy_hash") != selection_policy_hash:
            raise ValueCarrySelectionContractError("longlist row Policy hash does not match origin")
        if (
            row.get("rank") != rank
            or row.get("lane_rank") != rank
            or row.get("baseline_er_rank") != rank
        ):
            raise ValueCarrySelectionContractError("longlist ranks must be contiguous and aligned")
        if row.get("lane_native_unit") != "annual_ratio":
            raise ValueCarrySelectionContractError("Value / Carry native unit must be annual_ratio")
        native_value = row.get("lane_native_value")
        if (
            isinstance(native_value, bool)
            or not isinstance(native_value, int | float)
            or not isfinite(float(native_value))
        ):
            raise ValueCarrySelectionContractError("Value / Carry native value must be finite")
        expected_return_pct = row.get("expected_return_pct")
        if (
            isinstance(expected_return_pct, bool)
            or not isinstance(expected_return_pct, int | float)
            or not isfinite(float(expected_return_pct))
            or float(expected_return_pct) != round(float(native_value) * 100, 4)
        ):
            raise ValueCarrySelectionContractError(
                "longlist displayed E[r] does not match its native value"
            )
        if row.get("policy_diagnostic_ids") != []:
            raise ValueCarrySelectionContractError(
                "Value / Carry rows must carry the exact empty Policy Diagnostic set"
            )
    return policy_parameters


def value_carry_only_attention_policy_hash(
    *,
    selection_policy_hash: str,
    parameters: ValueCarryOnlyAttentionParameters,
) -> str:
    """Hash policy identity and runtime parameters, excluding dynamic Review Basis."""

    return _canonical_sha256(
        {
            "attention_policy_id": VALUE_CARRY_ONLY_ATTENTION_POLICY_ID,
            "input_selection_policy_hash": selection_policy_hash,
            "parameters": parameters.model_dump(mode="json"),
            "review_set_construction": "value-carry-lane-longlist-in-order",
        }
    )


def value_carry_expected_longlist(
    candidates: Sequence[Mapping[str, object]],
    *,
    parameters: ValueCarrySelectionPolicyParameters,
    asof_date: str,
) -> tuple[tuple[str, float, str | None, Mapping[str, object]], ...]:
    """Recompute the exact Value / Carry rank coordinates from source candidates."""

    required_jpx_flags = frozenset(parameters.required_jpx_flags)
    asof = date.fromisoformat(asof_date)
    ranked: list[
        tuple[tuple[object, ...], tuple[str, float, str | None, Mapping[str, object]]]
    ] = []
    for raw in candidates:
        candidate = candidate_record_from_mapping(raw)
        liquidity_facts = (
            candidate.market_cap_oku,
            candidate.avg_turnover_oku,
            candidate.listing_span_days,
            candidate.jpx_flags,
        )
        if any(value is None for value in liquidity_facts) or not (
            parameters.liquidity_parameters.matches(
                market_cap_oku=candidate.market_cap_oku,
                avg_turnover_oku=candidate.avg_turnover_oku,
                listing_span_days=candidate.listing_span_days,
                jpx_flags=candidate.jpx_flags,
                required_jpx_flags=required_jpx_flags,
                require_facts=True,
            )
        ):
            continue
        er_annual = optional_float(candidate.metrics.get("er_annual"))
        if er_annual is None:
            continue
        primary_pattern, _, strength_key = _best_selection_evidence(
            _sizing_eligible_evidence_hits(candidate.evidence_hits),
            evidence_pattern_order=parameters.evidence_pattern_order,
        )
        candidate_with_seed = {
            **raw,
            "candidate_diagnostics": _candidate_diagnostics(
                candidate, parameters.candidate_diagnostic_parameters
            ),
            "primary_evidence_pattern_id": primary_pattern,
            "deterioration_gate_unmeasurable": candidate.metrics.get(
                "deterioration_gate_unmeasurable"
            )
            is True,
            "decision_input_seed": _decision_input_seed(raw, asof_date=asof),
        }
        candidate_with_seed["reason_tags"] = _candidate_reason_tags(candidate_with_seed)
        candidate_with_seed["risk_tags"] = _candidate_risk_tags(candidate_with_seed)
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
                    _longlist_machine_projection(candidate_with_seed),
                ),
            )
        )
    ranked.sort(key=lambda item: item[0])
    return tuple(row for _, row in ranked[: parameters.lane_longlist_depth])


def _canonical_sha256(value: object) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _required_mapping(payload: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise ValueCarrySelectionContractError(f"{key} must be an object")
    return value


def _required_sha256(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueCarrySelectionContractError(f"{key} must be a SHA-256 digest")
    return value


def _required_tickers(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise ValueCarrySelectionContractError(f"{label} must be an array")
    tickers: list[str] = []
    for ticker in value:
        if not isinstance(ticker, str) or re.fullmatch(r"[0-9A-Z]{4}", ticker) is None:
            raise ValueCarrySelectionContractError(f"{label} contains an invalid ticker")
        tickers.append(ticker)
    if len(tickers) != len(set(tickers)):
        raise ValueCarrySelectionContractError(f"{label} contains duplicate tickers")
    return tuple(tickers)


__all__ = [
    "RESEARCH_GATE_CONTRACT_ID",
    "VALUE_CARRY_ONLY_ATTENTION_POLICY_ID",
    "VALUE_CARRY_OPPORTUNITY_LANE_ID",
    "VALUE_CARRY_SELECTION_POLICY_ID",
    "ValueCarryOnlyAttentionParameters",
    "ValueCarrySelectionContractError",
    "ValueCarrySelectionPolicyParameters",
    "validate_value_carry_selection_payload",
    "value_carry_expected_longlist",
    "value_carry_only_attention_policy_hash",
    "value_carry_selection_policy_hash",
    "value_carry_selection_policy_representation",
]
